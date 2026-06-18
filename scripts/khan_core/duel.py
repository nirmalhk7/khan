from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

from .agent_adapters import DEFAULT_AGENT_REGISTRY, AgentAdapterRegistry
from .agents import AgentSessionRunner
from .config import discover_project, load_config, save_config
from .models import AgentProvider, DuelParticipantRecord, DuelRecord, ProjectConfig
from .store import Store
from .validator import Validator


class DuelError(RuntimeError):
    pass


class DuelRunner:
    def __init__(self, config_path: Path | None = None, registry: AgentAdapterRegistry | None = None) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        self.store = Store(self.config.global_config.state_dir)
        self.registry = registry or DEFAULT_AGENT_REGISTRY
        self.validator = Validator()

    def run_duel(
        self,
        target: str,
        prompt: str,
        *,
        providers: list[AgentProvider] | None = None,
        validate: bool = True,
    ) -> DuelRecord:
        project = self._resolve_project(target)
        selected = providers or ["codex", "cursor-agent"]
        self._validate_providers(selected)
        duel = self.store.create_duel(project.name, prompt, selected)
        self.store.update_duel(duel.id, "running", f"Running duel across {', '.join(selected)}.")

        for provider in selected:
            self._run_participant(duel, project, provider, validate=validate)

        participants = self.store.list_duel_participants(duel.id)
        report = self._write_report(duel, participants)
        status = "awaiting_decision" if any(p.status == "succeeded" for p in participants) else "failed"
        summary = self._duel_summary(participants)
        self.store.update_duel(duel.id, status, summary, report_path=str(report))
        return self.store.get_duel(duel.id)

    def _run_participant(
        self,
        duel: DuelRecord,
        project: ProjectConfig,
        provider: AgentProvider,
        *,
        validate: bool,
    ) -> DuelParticipantRecord:
        self.store.upsert_duel_participant(duel.id, provider, "running")
        start = time.monotonic()
        session_id: str | None = None
        try:
            runner = AgentSessionRunner(self.config_path, registry=self.registry)
            runner.config = self.config
            runner.store = self.store
            session_id = runner.start_session(provider, project.name, self._participant_prompt(duel, provider), force_worktree=True)
            session = self.store.get_agent_session(session_id)
            workspace = Path(session.workspace)
            changed_files = self._changed_files(workspace)
            diff_stat = self._diff_stat(workspace, changed_files)
            validation = self.validator.run(workspace, project.validate_commands) if validate else None
            open_risks = self._open_risks(session.summary, changed_files, validation.ok if validation else None)
            artifact = self._write_participant_artifact(
                duel,
                provider,
                session_id,
                workspace,
                session.status,
                session.summary,
                changed_files,
                diff_stat,
                validation.summary if validation else "Validation skipped.",
                open_risks,
            )
            return self.store.upsert_duel_participant(
                duel.id,
                provider,
                "succeeded" if session.status == "succeeded" else "failed",
                session_id=session_id,
                workspace=str(workspace),
                changed_files=changed_files,
                diff_stat=diff_stat,
                validation_ok=validation.ok if validation else None,
                validation_summary=validation.summary if validation else "Validation skipped.",
                runtime_seconds=time.monotonic() - start,
                summary=session.summary,
                open_risks=open_risks,
                artifact_path=str(artifact),
            )
        except Exception as exc:
            summary = f"{type(exc).__name__}: {exc}"
            artifact = self.store.write_artifact(
                duel.id,
                f"{self._safe_provider(provider)}-result.md",
                f"# {provider} Result\n\nStatus: failed\n\n{summary}\n",
            )
            return self.store.upsert_duel_participant(
                duel.id,
                provider,
                "failed",
                session_id=session_id,
                runtime_seconds=time.monotonic() - start,
                summary=summary,
                open_risks=[summary],
                artifact_path=str(artifact),
            )

    def _resolve_project(self, target: str) -> ProjectConfig:
        if target in self.config.projects:
            return self.config.projects[target]

        path = Path(target).expanduser().resolve()
        if not path.exists():
            raise DuelError(f"Project target is neither a configured project nor an existing path: {target}")
        root = self._git_root(path)
        if root is None:
            raise DuelError("Provider duels require a git repository so candidates can run in isolated worktrees.")

        for project in self.config.projects.values():
            if project.path == root:
                return project

        project_name = self._generated_project_name(root)
        project = discover_project(root, project_name)
        project.workspace_mode = "worktree"
        self.config.projects[project.name] = project
        save_config(self.config, self.config_path)
        return project

    def _generated_project_name(self, root: Path) -> str:
        base = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in root.name).strip("-") or "project"
        digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:8]
        candidate = f"local-{base}-{digest}"
        if candidate not in self.config.projects:
            return candidate
        index = 2
        while f"{candidate}-{index}" in self.config.projects:
            index += 1
        return f"{candidate}-{index}"

    def _validate_providers(self, providers: list[AgentProvider]) -> None:
        missing = [provider for provider in providers if provider not in self.registry.names()]
        if missing:
            raise DuelError(f"Unknown provider(s): {', '.join(missing)}")
        if len(set(providers)) != len(providers):
            raise DuelError("Duel providers must be unique.")

    def _participant_prompt(self, duel: DuelRecord, provider: AgentProvider) -> str:
        return (
            f"You are provider {provider} in Khan duel {duel.id}.\n"
            "Implement the task independently in this isolated workspace.\n"
            "Run relevant tests or explain why they could not be run.\n"
            "Finish with a concise summary and any open risks.\n\n"
            f"Task:\n{duel.prompt}\n"
        )

    def _write_participant_artifact(
        self,
        duel: DuelRecord,
        provider: AgentProvider,
        session_id: str,
        workspace: Path,
        status: str,
        summary: str,
        changed_files: list[str],
        diff_stat: str,
        validation_summary: str,
        open_risks: list[str],
    ) -> Path:
        events = self.store.list_agent_session_events(session_id, limit=200)
        lines = [
            f"# {provider} Result",
            "",
            f"- Duel: `{duel.id}`",
            f"- Session: `{session_id}`",
            f"- Status: `{status}`",
            f"- Workspace: `{workspace}`",
            f"- Validation: {validation_summary}",
            "",
            "## Summary",
            "",
            summary or "_No summary recorded._",
            "",
            "## Changed Files",
            "",
        ]
        lines.extend(f"- `{path}`" for path in changed_files)
        if not changed_files:
            lines.append("- _No files changed._")
        lines.extend(["", "## Diff Stat", "", "```text", diff_stat or "No diff.", "```", "", "## Open Risks", ""])
        lines.extend(f"- {risk}" for risk in open_risks)
        if not open_risks:
            lines.append("- _None recorded._")
        lines.extend(["", "## Transcript", ""])
        for event in events:
            payload = json.dumps(event.payload, sort_keys=True) if event.payload else "{}"
            lines.append(f"- `{event.stream}` {event.ts.isoformat()} {event.message} `{payload}`")
        return self.store.write_artifact(duel.id, f"{self._safe_provider(provider)}-result.md", "\n".join(lines) + "\n")

    def _write_report(self, duel: DuelRecord, participants: list[DuelParticipantRecord]) -> Path:
        lines = [
            "# Duel Report",
            "",
            f"- Duel: `{duel.id}`",
            f"- Project: `{duel.project}`",
            f"- Providers: {', '.join(duel.providers)}",
            "",
            "## Task",
            "",
            duel.prompt,
            "",
            "## Provider Comparison",
            "",
            "| Provider | Status | Validation | Changed Files | Runtime | Workspace |",
            "| --- | --- | --- | ---: | ---: | --- |",
        ]
        for participant in participants:
            validation = (
                "skipped" if participant.validation_ok is None else "pass" if participant.validation_ok else "fail"
            )
            lines.append(
                "| "
                f"{participant.provider} | {participant.status} | {validation} | "
                f"{len(participant.changed_files)} | {participant.runtime_seconds:.2f}s | "
                f"`{participant.workspace or '-'}` |"
            )

        lines.extend(["", "## Recommendation", "", self._recommendation(participants), "", "## Participant Details", ""])
        for participant in participants:
            lines.extend(
                [
                    f"### {participant.provider}",
                    "",
                    f"- Session: `{participant.session_id or '-'}`",
                    f"- Artifact: `{participant.artifact_path or '-'}`",
                    f"- Validation: {participant.validation_summary or 'not recorded'}",
                    "",
                    participant.summary or "_No summary recorded._",
                    "",
                ]
            )
            if participant.open_risks:
                lines.append("Risks:")
                lines.extend(f"- {risk}" for risk in participant.open_risks)
                lines.append("")
        return self.store.write_artifact(duel.id, "duel-report.md", "\n".join(lines) + "\n")

    def _duel_summary(self, participants: list[DuelParticipantRecord]) -> str:
        counts: dict[str, int] = {}
        for participant in participants:
            counts[participant.status] = counts.get(participant.status, 0) + 1
        status_bits = ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))
        return f"Duel complete; awaiting operator decision ({status_bits})."

    def _recommendation(self, participants: list[DuelParticipantRecord]) -> str:
        passing = [p for p in participants if p.status == "succeeded" and p.validation_ok is not False]
        if len(passing) == 1:
            return f"Inspect and consider adopting `{passing[0].provider}`; it is the only candidate without validation failure."
        if len(passing) > 1:
            return "Multiple candidates completed without validation failure. Compare changed files and summaries before adopting."
        return "No candidate completed cleanly. Inspect participant artifacts before retrying or changing the prompt."

    def _open_risks(self, summary: str, changed_files: list[str], validation_ok: bool | None) -> list[str]:
        risks: list[str] = []
        if not changed_files:
            risks.append("No files changed.")
        if validation_ok is False:
            risks.append("Validation failed.")
        lowered = summary.lower()
        for marker in ("risk", "todo", "blocked", "cannot", "could not", "failed"):
            if marker in lowered:
                risks.append(f"Provider summary mentions `{marker}`.")
                break
        return risks

    def _git_root(self, path: Path) -> Path | None:
        process = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path if path.is_dir() else path.parent,
            text=True,
            capture_output=True,
            check=False,
        )
        if process.returncode != 0:
            return None
        return Path(process.stdout.strip()).resolve()

    def _changed_files(self, workspace: Path) -> list[str]:
        status = self._git_text(workspace, "status", "--porcelain", "--untracked-files=all")
        files: list[str] = []
        for line in status.splitlines():
            if not line:
                continue
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            if path:
                files.append(path)
        return files

    def _diff_stat(self, workspace: Path, changed_files: list[str]) -> str:
        diff_stat = self._git_text(workspace, "diff", "--stat")
        untracked = self._git_lines(workspace, "ls-files", "--others", "--exclude-standard")
        if untracked:
            untracked_stat = "\n".join(f" {path} | untracked" for path in untracked)
            return "\n".join(part for part in (diff_stat, untracked_stat) if part)
        if not diff_stat and changed_files:
            return "\n".join(f" {path} | changed" for path in changed_files)
        return diff_stat

    def _git_lines(self, workspace: Path, *args: str) -> list[str]:
        text = self._git_text(workspace, *args)
        return [line for line in text.splitlines() if line]

    def _git_text(self, workspace: Path, *args: str) -> str:
        if not workspace.exists():
            return ""
        process = subprocess.run(["git", *args], cwd=workspace, text=True, capture_output=True, check=False)
        return process.stdout.strip() if process.returncode == 0 else process.stderr.strip()

    @staticmethod
    def _safe_provider(provider: str) -> str:
        return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in provider)

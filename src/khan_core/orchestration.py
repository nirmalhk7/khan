from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .agents import AgentSessionRunner
from .config import discover_project, load_config, save_config
from .loop_engine import LoopEngine
from .models import AgentProvider, ProjectConfig, RunRecord, TaskCapsule, TaskRecord
from .prompt_builder import build_worker_prompt
from .store import Store
from .validator import Validator


@dataclass(frozen=True)
class RelayOutcome:
    first_session_id: str
    second_session_id: str
    handoff_path: Path


@dataclass(frozen=True)
class SteerOutcome:
    session_id: str
    forked: bool
    prompt_path: Path


@dataclass(frozen=True)
class ReplayOutcome:
    source_run_id: str
    run_id: str
    provider: AgentProvider
    status: str
    summary: str
    artifact_path: Path | None = None
    metadata_path: Path | None = None
    score: int | None = None
    score_notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BenchOutcome:
    results: list[ReplayOutcome]
    json_path: Path | None = None
    markdown_path: Path | None = None


class OrchestrationError(RuntimeError):
    pass


class OrchestrationRunner:
    RELAY_PRESETS: dict[str, tuple[AgentProvider, AgentProvider]] = {
        "codex-plan cursor-build": ("codex", "cursor-agent"),
        "cursor-build codex-review": ("cursor-agent", "codex"),
        "codex-fix cursor-polish": ("codex", "cursor-agent"),
    }

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        self.store = Store(self.config.global_config.state_dir)
        self.sessions = AgentSessionRunner(config_path)
        self.validator = Validator()

    def _resolve_project(self, target: str, *, prefix: str) -> ProjectConfig:
        if target in self.config.projects:
            return self.config.projects[target]

        path = Path(target).expanduser().resolve()
        if not path.exists():
            raise OrchestrationError(f"Target is neither a configured project nor an existing path: {target}")
        root = self._git_root(path)
        if root is None:
            raise OrchestrationError("Target path must be inside a git repository.")

        for project in self.config.projects.values():
            if project.path == root:
                return project

        project = discover_project(root, self._generated_project_name(root, prefix))
        self.config.projects[project.name] = project
        save_config(self.config, self.config_path)
        return project

    def relay(
        self,
        target: str,
        prompt: str,
        *,
        first: AgentProvider | None = None,
        second: AgentProvider | None = None,
        preset: str | None = None,
    ) -> RelayOutcome:
        if preset:
            try:
                first, second = self.RELAY_PRESETS[preset]
            except KeyError as exc:
                raise OrchestrationError(
                    f"Unknown relay preset: {preset}. Available presets: {', '.join(self.RELAY_PRESETS)}"
                ) from exc
        if first is None or second is None:
            raise OrchestrationError("Relay requires either explicit providers or a named preset.")
        project = self._resolve_project(target, prefix="relay")
        first_prompt = self._relay_prompt("first", first, second, prompt)
        first_session_id = self.sessions.start_session(first, project.name, first_prompt, force_worktree=True)
        first_session = self.store.get_agent_session(first_session_id)
        handoff_prompt = self._handoff_prompt(first_session_id, first_session.summary, prompt, second)
        handoff_path = self.store.write_artifact(first_session_id, "relay-handoff.md", handoff_prompt)
        if preset:
            self.store.write_artifact(
                first_session_id,
                "relay-preset.md",
                self._relay_preset_artifact(preset, first, second, prompt, handoff_prompt),
            )
        second_session_id = self.sessions.start_session(
            second,
            project.name,
            handoff_prompt,
            workspace=Path(first_session.workspace),
            parent_session_id=first_session_id,
        )
        self.store.write_artifact(second_session_id, "relay-handoff.md", handoff_prompt)
        if preset:
            self.store.write_artifact(
                second_session_id,
                "relay-preset.md",
                self._relay_preset_artifact(preset, first, second, prompt, handoff_prompt),
            )
        return RelayOutcome(first_session_id, second_session_id, handoff_path)

    def steer(self, session_id: str, message: str) -> SteerOutcome:
        session = self.store.get_agent_session(session_id)
        project = self._project(session.project)
        continuation = self._steer_prompt(session, message)
        prompt_path = self.store.write_artifact(session.id, "steer-message.md", continuation)
        forked = session.status in {"running", "queued", "stopping"}
        next_session_id = self.sessions.start_session(
            session.provider,
            project.name,
            continuation,
            force_worktree=forked,
            workspace=None if forked else Path(session.workspace),
            parent_session_id=session.id,
            resume_external_session_id=session.external_id,
            resume_message=message,
        )
        return SteerOutcome(next_session_id, forked, prompt_path)

    def replay(self, run_id: str, provider: AgentProvider) -> ReplayOutcome:
        run = self.store.get_run(run_id)
        task = self.store.get_task(run.task_id)
        project = self._project(task.project)
        capsule = self.store.get_task_capsule(task.id)
        if provider == "codex":
            engine = LoopEngine(self.config_path)
            replay_run_id = engine.retry_run(run_id)
            artifact = self.store.write_artifact(
                replay_run_id,
                "replay.json",
                json.dumps(
                    {
                        "source_run_id": run_id,
                        "provider": provider,
                        "mode": "codex-retry",
                    },
                    indent=2,
                ),
            )
            replay_run = self.store.get_run(replay_run_id)
            metadata = self.store.write_artifact(
                replay_run.id,
                "replay-metadata.json",
                self._replay_metadata(
                    source_run_id=run_id,
                    provider=provider,
                    run=replay_run,
                task=task,
                project=project,
                capsule=capsule,
                validation_summary="Codex retry handled inside the task loop.",
            ),
            )
            self.store.write_artifact(replay_run.id, "replay-prompt.md", self._replay_prompt(task.title, task.prompt, task.success_criteria, project, capsule, run))
            score, score_notes = self._score_run(
                replay_run,
                source_run_id=run_id,
                task=task,
                project=project,
                capsule=capsule,
            )
            return ReplayOutcome(run_id, replay_run.id, provider, replay_run.status, replay_run.summary, artifact, metadata, score, score_notes)

        workspace = Path(run.workspace)
        force_worktree = not workspace.exists()
        prompt = self._replay_prompt(task.title, task.prompt, task.success_criteria, project, capsule, run)
        session_id = self.sessions.start_session(
            provider,
            project.name,
            prompt,
            force_worktree=force_worktree,
            workspace=None if force_worktree else workspace,
            parent_session_id=None,
        )
        session = self.store.get_agent_session(session_id)
        active_workspace = Path(session.workspace)
        replay_run = self.store.create_run(task.id, project.name, str(active_workspace))
        self.store.update_run(replay_run.id, "running", f"Replaying with {provider}.")
        validation = self.validator.run(active_workspace, capsule.verification or project.validate_commands)
        status = "succeeded" if session.status == "succeeded" and validation.ok else "failed"
        summary = f"{provider} replay: {session.summary or status}. {validation.summary}"
        self.store.set_session_id(replay_run.id, session_id)
        self.store.update_run(replay_run.id, status, summary)
        artifact = self.store.write_artifact(
            replay_run.id,
            "replay-summary.md",
            self._replay_summary(provider, run_id, task.prompt, session.summary, validation.summary, active_workspace),
        )
        self.store.write_artifact(replay_run.id, "replay-prompt.md", prompt)
        metadata = self.store.write_artifact(
            replay_run.id,
            "replay-metadata.json",
            self._replay_metadata(
                source_run_id=run_id,
                provider=provider,
                run=self.store.get_run(replay_run.id),
                task=task,
                project=project,
                capsule=capsule,
                validation_summary=validation.summary,
            ),
        )
        score, score_notes = self._score_run(
            self.store.get_run(replay_run.id),
            source_run_id=run_id,
            task=task,
            project=project,
            capsule=capsule,
            validation_summary=validation.summary,
            review_summary=session.summary,
        )
        return ReplayOutcome(run_id, replay_run.id, provider, status, summary, artifact, metadata, score, score_notes)

    def bench(self, path: Path, provider: AgentProvider | None = None) -> BenchOutcome:
        raw = yaml.safe_load(path.read_text()) or {}
        entries = raw if isinstance(raw, list) else raw.get("runs", [])
        results: list[ReplayOutcome] = []
        for entry in entries:
            target = entry["target"]
            prompt = entry["prompt"]
            chosen = entry.get("provider") or provider or "codex"
            if entry.get("kind") == "relay":
                relay = self.relay(
                    target,
                    prompt,
                    first=entry.get("first", "codex"),
                    second=entry.get("second", "cursor-agent"),
                    preset=entry.get("preset"),
                )
                results.append(
                    ReplayOutcome(
                        source_run_id=relay.first_session_id,
                        run_id=relay.second_session_id,
                        provider=chosen,
                        status="succeeded",
                        summary=f"Relay {relay.first_session_id[:8]} -> {relay.second_session_id[:8]}",
                        artifact_path=relay.handoff_path,
                        metadata_path=relay.handoff_path,
                        score=100,
                        score_notes=["relay handoff"],
                    )
                )
                continue
            replay = self.replay(target, chosen)
            results.append(replay)
        markdown = self.store.write_artifact(
            f"bench-{uuid.uuid4()}",
            "bench-report.md",
            self._bench_markdown(results),
        )
        json_path = self.store.write_artifact(
            markdown.parent.name,
            "bench-report.json",
            json.dumps([self._bench_result_payload(result) for result in results], default=str, indent=2),
        )
        return BenchOutcome(results, json_path=json_path, markdown_path=markdown)

    def _project(self, project_name: str) -> ProjectConfig:
        if project_name not in self.config.projects:
            raise OrchestrationError(f"Project not found in config: {project_name}")
        return self.config.projects[project_name]

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

    def _generated_project_name(self, root: Path, prefix: str) -> str:
        base = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in root.name).strip("-") or "project"
        digest = uuid.uuid5(uuid.NAMESPACE_URL, str(root)).hex[:8]
        candidate = f"{prefix}-{base}-{digest}"
        if candidate not in self.config.projects:
            return candidate
        index = 2
        while f"{candidate}-{index}" in self.config.projects:
            index += 1
        return f"{candidate}-{index}"

    def _relay_prompt(self, phase: str, first: AgentProvider, second: AgentProvider, prompt: str) -> str:
        return (
            f"Relay phase {phase}.\n"
            f"Provider pair: {first} -> {second}\n\n"
            f"Original task:\n{prompt}\n"
        )

    def _relay_preset_artifact(
        self,
        preset: str,
        first: AgentProvider,
        second: AgentProvider,
        prompt: str,
        handoff_prompt: str,
    ) -> str:
        return (
            f"# Relay Preset\n\n"
            f"- Preset: `{preset}`\n"
            f"- First: `{first}`\n"
            f"- Second: `{second}`\n\n"
            f"## Original Prompt\n\n"
            f"```text\n{prompt}\n```\n\n"
            f"## Handoff Prompt\n\n"
            f"```text\n{handoff_prompt}\n```\n"
        )

    def _handoff_prompt(self, session_id: str, summary: str, prompt: str, second: AgentProvider) -> str:
        events = self.store.list_agent_session_events(session_id, limit=50)
        transcript = "\n".join(f"- {event.stream}: {event.message}" for event in events)
        return (
            f"You are continuing a Khan relay as provider {second}.\n"
            f"Previous session summary: {summary or 'No summary recorded.'}\n\n"
            f"Original task:\n{prompt}\n\n"
            f"Transcript:\n{transcript or '- No transcript recorded.'}\n"
        )

    def _steer_prompt(self, session, message: str) -> str:
        events = self.store.list_agent_session_events(session.id, limit=50)
        transcript = "\n".join(f"- {event.stream}: {event.message}" for event in events)
        return (
            f"You are continuing session {session.id} for provider {session.provider}.\n"
            f"Original prompt:\n{session.prompt}\n\n"
            f"New user message:\n{message}\n\n"
            f"Recent transcript:\n{transcript or '- No transcript recorded.'}\n"
        )

    def _replay_prompt(
        self,
        title: str,
        prompt: str,
        success_criteria: str,
        project: ProjectConfig,
        capsule,
        run,
    ) -> str:
        task = type("ReplayTask", (), {"title": title, "prompt": prompt, "success_criteria": success_criteria})()
        return build_worker_prompt(task, project, run.iteration, [], [], capsule)

    def _replay_summary(
        self,
        provider: AgentProvider,
        source_run_id: str,
        prompt: str,
        session_summary: str,
        validation_summary: str,
        workspace: Path,
    ) -> str:
        return (
            f"# Replay Summary\n\n"
            f"- Provider: `{provider}`\n"
            f"- Source run: `{source_run_id}`\n"
            f"- Workspace: `{workspace}`\n"
            f"- Validation: {validation_summary}\n\n"
            f"## Session Summary\n\n"
            f"{session_summary or '_No session summary recorded._'}\n\n"
            f"## Prompt\n\n"
            f"```text\n{prompt}\n```\n"
        )

    def _bench_markdown(self, results: list[ReplayOutcome]) -> str:
        lines = [
            "# Bench Report",
            "",
            "| Source Run | Run | Provider | Status | Score | Summary | Notes |",
            "| --- | --- | --- | --- | ---: | --- | --- |",
        ]
        for result in results:
            lines.append(
                f"| `{result.source_run_id}` | `{result.run_id}` | {result.provider} | {result.status} | "
                f"{result.score if result.score is not None else '-'} | {result.summary} | "
                f"{'; '.join(result.score_notes) if result.score_notes else '-'} |"
            )
        return "\n".join(lines) + "\n"

    def _replay_metadata(
        self,
        *,
        source_run_id: str,
        provider: AgentProvider,
        run,
        task,
        project: ProjectConfig,
        capsule,
        validation_summary: str,
    ) -> str:
        return json.dumps(
            {
                "source_run_id": source_run_id,
                "replay_run_id": run.id,
                "provider": provider,
                "status": run.status,
                "summary": run.summary,
                "iteration": run.iteration,
                "project": project.name,
                "task": task.model_dump(mode="json"),
                "capsule": capsule.model_dump(mode="json"),
                "validation_summary": validation_summary,
            },
            indent=2,
        )

    def _score_run(self, status: str, iteration: int, summary: str) -> int:
        score = 100 if status == "succeeded" else 35
        score -= max(iteration - 1, 0) * 5
        if "validation failed" in summary.lower():
            score -= 20
        if "validation skipped" in summary.lower():
            score -= 10
        return max(score, 0)

    def _bench_result_payload(self, result: ReplayOutcome) -> dict[str, object]:
        return {
            "source_run_id": result.source_run_id,
            "run_id": result.run_id,
            "provider": result.provider,
            "status": result.status,
            "summary": result.summary,
            "artifact_path": str(result.artifact_path) if result.artifact_path else "",
            "metadata_path": str(result.metadata_path) if result.metadata_path else "",
            "score": result.score,
            "score_notes": result.score_notes,
        }

    def _score_run(
        self,
        run: RunRecord,
        *,
        source_run_id: str,
        task: TaskRecord,
        project: ProjectConfig,
        capsule: TaskCapsule,
        validation_summary: str | None = None,
        review_summary: str | None = None,
    ) -> tuple[int, list[str]]:
        events = self.store.list_events(run.id, limit=200)
        score = 50
        notes: list[str] = []

        if run.status == "succeeded":
            score += 30
            notes.append("run succeeded")
        else:
            score -= 20
            notes.append(f"run status {run.status}")

        if run.iteration <= 1:
            score += 10
            notes.append("single iteration")
        else:
            penalty = min((run.iteration - 1) * 5, 20)
            score -= penalty
            notes.append(f"iteration penalty={penalty}")

        validation_text = validation_summary or self._event_message(events, "validating") or ""
        if "passed" in validation_text.lower():
            score += 15
            notes.append("validation passed")
        elif "failed" in validation_text.lower():
            score -= 15
            notes.append("validation failed")
        elif validation_text:
            notes.append("validation recorded")

        review_text = review_summary or self._event_message(events, "reviewing") or ""
        review_verdict = self._extract_verdict(review_text)
        if review_verdict == "PASS":
            score += 10
            notes.append("review PASS")
        elif review_verdict == "FIX":
            score -= 10
            notes.append("review FIX")
        elif review_verdict == "ESCALATE":
            score -= 20
            notes.append("review ESCALATE")

        changed_files = self._changed_files(Path(run.workspace)) if Path(run.workspace).exists() else []
        protected = [path for path in changed_files if self._matches_any(path, [*project.protected_paths, *capsule.protected_paths])]
        allowed = [path for path in changed_files if capsule.allowed_paths and not self._matches_any(path, capsule.allowed_paths)]
        if protected:
            score -= len(protected) * 15
            notes.append(f"protected-path hits={len(protected)}")
        else:
            score += 5
            notes.append("no protected-path hits")
        if allowed:
            score -= len(allowed) * 10
            notes.append(f"outside-allowed={len(allowed)}")
        elif capsule.allowed_paths:
            score += 5
            notes.append("inside allowed paths")

        adoption = [
            decision
            for decision in self.store.list_adoption_decisions(limit=200)
            if decision.target_id in {source_run_id, run.id} or decision.session_id == run.session_id
        ]
        if any(decision.status == "adopted" for decision in adoption):
            score += 15
            notes.append("human adopted")
        elif any(decision.status == "rejected" for decision in adoption):
            score -= 10
            notes.append("human rejected")

        runtime_seconds = max((run.updated_at - run.created_at).total_seconds(), 0.0)
        runtime_penalty = min(int(runtime_seconds // 30), 20)
        if runtime_penalty:
            score -= runtime_penalty
            notes.append(f"runtime penalty={runtime_penalty}")

        return max(score, 0), notes

    def _event_message(self, events: list, phase: str) -> str:
        for event in reversed(events):
            if getattr(event, "phase", None) == phase:
                return event.message
        return ""

    def _extract_verdict(self, text: str) -> str:
        lowered = text.lower()
        if "verdict: pass" in lowered or lowered.endswith("pass"):
            return "PASS"
        if "verdict: fix" in lowered or lowered.endswith("fix"):
            return "FIX"
        if "verdict: escalate" in lowered or lowered.endswith("escalate"):
            return "ESCALATE"
        return ""

    def _matches_any(self, path: str, prefixes: list[str]) -> bool:
        return any(path == prefix or path.startswith(f"{prefix.rstrip('/')}/") for prefix in prefixes if prefix)

    def _changed_files(self, workspace: Path) -> list[str]:
        process = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=workspace,
            text=True,
            capture_output=True,
            check=False,
        )
        if process.returncode != 0:
            return []
        files: list[str] = []
        for line in process.stdout.splitlines():
            if not line:
                continue
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            if path:
                files.append(path)
        return files

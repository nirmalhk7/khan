from __future__ import annotations

import json
import subprocess
import re
from pathlib import Path

from .agent_adapters import DEFAULT_AGENT_REGISTRY, AgentAdapterRegistry
from .agents import AgentSessionRunner
from .config import load_config
from .models import AgentProvider, CrossReviewCritiqueRecord, CrossReviewRecord, DuelParticipantRecord, ProjectConfig
from .store import Store


class CrossReviewError(RuntimeError):
    pass


class CrossReviewRunner:
    def __init__(self, config_path: Path | None = None, registry: AgentAdapterRegistry | None = None) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        self.store = Store(self.config.global_config.state_dir)
        self.registry = registry or DEFAULT_AGENT_REGISTRY

    def run_cross_review(self, duel_id: str) -> CrossReviewRecord:
        duel = self.store.get_duel(duel_id)
        project = self._project(duel.project)
        participants = self._reviewable_participants(duel_id)
        record = self.store.create_cross_review(duel_id)
        self.store.update_cross_review(record.id, "running", f"Running cross-review for duel {duel_id}.")

        for reviewer in participants:
            for subject in participants:
                if reviewer.provider == subject.provider:
                    continue
                self._run_critique(record, project, reviewer, subject)

        critiques = self.store.list_cross_review_critiques(record.id)
        self._finalize_critiques(record.id, critiques)
        critiques = self.store.list_cross_review_critiques(record.id)
        report = self._write_report(record, critiques)
        status = "awaiting_decision" if any(c.status == "succeeded" for c in critiques) else "failed"
        summary = self._summary(critiques)
        self.store.update_cross_review(record.id, status, summary, report_path=str(report))
        return self.store.get_cross_review(record.id)

    def _run_critique(
        self,
        record: CrossReviewRecord,
        project: ProjectConfig,
        reviewer: DuelParticipantRecord,
        subject: DuelParticipantRecord,
    ) -> CrossReviewCritiqueRecord:
        self.store.upsert_cross_review_critique(
            record.id,
            record.duel_id,
            reviewer.provider,
            subject.provider,
            "running",
        )
        try:
            diff_text = self._candidate_diff(Path(subject.workspace))
            prompt = self._review_prompt(project, record.duel_id, reviewer.provider, subject, diff_text)
            runner = AgentSessionRunner(self.config_path, registry=self.registry)
            runner.config = self.config
            runner.store = self.store
            session_id = runner.start_session(reviewer.provider, project.name, prompt, force_worktree=True)
            session = self.store.get_agent_session(session_id)
            findings = self._findings(session.summary)
            analysis = self._review_analysis(session.summary, reviewer.provider, subject.provider)
            artifact = self._write_critique_artifact(record, reviewer.provider, subject, session_id, prompt, session.summary, findings)
            status = "succeeded" if session.status == "succeeded" else "failed"
            return self.store.upsert_cross_review_critique(
                record.id,
                record.duel_id,
                reviewer.provider,
                subject.provider,
                status,
                session_id=session_id,
                verdict=analysis["verdict"],
                strongest_implementation=analysis["strongest_implementation"],
                reviewer_disagreement=analysis["reviewer_disagreement"],
                required_human_inspection=analysis["required_human_inspection"],
                summary=session.summary,
                findings=findings,
                artifact_path=str(artifact),
            )
        except Exception as exc:
            summary = f"{type(exc).__name__}: {exc}"
            artifact = self.store.write_artifact(
                record.id,
                f"{self._safe_provider(reviewer.provider)}-reviews-{self._safe_provider(subject.provider)}.md",
                f"# {reviewer.provider} Reviews {subject.provider}\n\nStatus: failed\n\n{summary}\n",
            )
            return self.store.upsert_cross_review_critique(
                record.id,
                record.duel_id,
                reviewer.provider,
                subject.provider,
                "failed",
                verdict="ESCALATE",
                strongest_implementation=None,
                reviewer_disagreement=True,
                required_human_inspection=True,
                summary=summary,
                findings=[summary],
                artifact_path=str(artifact),
            )

    def _reviewable_participants(self, duel_id: str) -> list[DuelParticipantRecord]:
        participants = [
            participant for participant in self.store.list_duel_participants(duel_id)
            if participant.status in {"succeeded", "adopted"} and participant.workspace
        ]
        if len(participants) < 2:
            raise CrossReviewError("Cross-review requires at least two completed duel participants.")
        providers = set(self.registry.names())
        missing = [participant.provider for participant in participants if participant.provider not in providers]
        if missing:
            raise CrossReviewError(f"Cannot review with unknown provider(s): {', '.join(sorted(missing))}")
        return participants

    def _project(self, project_name: str) -> ProjectConfig:
        try:
            return self.config.projects[project_name]
        except KeyError as exc:
            raise CrossReviewError(f"Project not found in config: {project_name}") from exc

    def _review_prompt(
        self,
        project: ProjectConfig,
        duel_id: str,
        reviewer_provider: AgentProvider,
        subject: DuelParticipantRecord,
        diff_text: str,
    ) -> str:
        return (
            f"{project.review_prompt}\n\n"
            f"You are `{reviewer_provider}` reviewing `{subject.provider}` for Khan duel `{duel_id}`.\n"
            "Review only. Do not edit files. Focus on correctness, regressions, missing tests, safety, and adoption risk.\n"
            "Return concrete findings and a concise recommendation.\n\n"
            "## Subject Summary\n"
            f"{subject.summary or 'No summary recorded.'}\n\n"
            "## Subject Changed Files\n"
            + "\n".join(f"- {path}" for path in subject.changed_files)
            + "\n\n## Subject Diff\n"
            "```diff\n"
            f"{diff_text or 'No diff available.'}\n"
            "```\n"
        )

    def _candidate_diff(self, workspace: Path) -> str:
        if not workspace.exists():
            raise CrossReviewError(f"Participant workspace does not exist: {workspace}")
        tracked = self._git_text(workspace, "diff", "--")
        untracked = self._git_lines(workspace, "ls-files", "--others", "--exclude-standard")
        chunks = [tracked] if tracked else []
        for path in untracked:
            file_path = workspace / path
            if file_path.is_file():
                chunks.append(f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n{self._added_file_lines(file_path)}")
            else:
                chunks.append(f"Untracked directory: {path}")
        return "\n\n".join(chunks)

    def _write_critique_artifact(
        self,
        record: CrossReviewRecord,
        reviewer_provider: AgentProvider,
        subject: DuelParticipantRecord,
        session_id: str,
        prompt: str,
        summary: str,
        findings: list[str],
    ) -> Path:
        events = self.store.list_agent_session_events(session_id, limit=200)
        lines = [
            f"# {reviewer_provider} Reviews {subject.provider}",
            "",
            f"- Cross-review: `{record.id}`",
            f"- Duel: `{record.duel_id}`",
            f"- Review session: `{session_id}`",
            f"- Subject session: `{subject.session_id or '-'}`",
            "",
            "## Findings",
            "",
        ]
        lines.extend(f"- {finding}" for finding in findings)
        if not findings:
            lines.append("- _No findings parsed._")
        lines.extend(["", "## Summary", "", summary or "_No summary recorded._", "", "## Prompt", "", "```text", prompt, "```", "", "## Transcript", ""])
        for event in events:
            payload = json.dumps(event.payload, sort_keys=True) if event.payload else "{}"
            lines.append(f"- `{event.stream}` {event.ts.isoformat()} {event.message} `{payload}`")
        lines.extend(
            [
                "",
                "## Parsed Decision",
                "",
                f"- Verdict: `{self._parse_verdict(summary)}`",
                f"- Strongest implementation: `{self._parse_strongest(summary, subject.provider) or '-'}`",
                f"- Reviewer disagreement: `{self._parse_disagreement(summary)}`",
                f"- Human inspection required: `{self._parse_human_inspection(summary)}`",
            ]
        )
        return self.store.write_artifact(
            record.id,
            f"{self._safe_provider(reviewer_provider)}-reviews-{self._safe_provider(subject.provider)}.md",
            "\n".join(lines) + "\n",
        )

    def _write_report(self, record: CrossReviewRecord, critiques: list[CrossReviewCritiqueRecord]) -> Path:
        lines = [
            "# Cross-Review Report",
            "",
            f"- Cross-review: `{record.id}`",
            f"- Duel: `{record.duel_id}`",
            "",
            "| Reviewer | Subject | Status | Findings | Artifact |",
            "| --- | --- | --- | ---: | --- |",
        ]
        for critique in critiques:
            lines.append(
                f"| {critique.reviewer_provider} | {critique.subject_provider} | {critique.status} | "
                f"{len(critique.findings)} | `{critique.artifact_path or '-'}` |"
            )
        lines.extend(["", "## Decision Card", "", self._decision_card(critiques), ""])
        for critique in critiques:
            lines.extend(
                [
                    f"## {critique.reviewer_provider} on {critique.subject_provider}",
                    "",
                    f"- Verdict: `{critique.verdict}`",
                    f"- Strongest implementation: `{critique.strongest_implementation or '-'}`",
                    f"- Reviewer disagreement: `{critique.reviewer_disagreement}`",
                    f"- Human inspection required: `{critique.required_human_inspection}`",
                    "",
                    critique.summary or "_No summary recorded._",
                    "",
                ]
            )
            if critique.findings:
                lines.append("Findings:")
                lines.extend(f"- {finding}" for finding in critique.findings)
                lines.append("")
        return self.store.write_artifact(record.id, "cross-review-report.md", "\n".join(lines) + "\n")

    def _summary(self, critiques: list[CrossReviewCritiqueRecord]) -> str:
        succeeded = sum(1 for critique in critiques if critique.status == "succeeded")
        failed = sum(1 for critique in critiques if critique.status == "failed")
        verdicts = ", ".join(
            f"{verdict}={sum(1 for critique in critiques if critique.verdict == verdict)}"
            for verdict in ("PASS", "FIX", "ESCALATE")
        )
        return f"Cross-review complete; awaiting operator decision (succeeded={succeeded}, failed={failed}, {verdicts})."

    def _decision_card(self, critiques: list[CrossReviewCritiqueRecord]) -> str:
        if not critiques:
            return "No critiques were recorded."
        failed = [critique for critique in critiques if critique.status == "failed"]
        if failed:
            return "At least one critique failed. Inspect failed critique artifacts before adopting."
        if any(critique.required_human_inspection for critique in critiques):
            return "One or more critiques require human inspection before adopting."
        strongest = {critique.strongest_implementation for critique in critiques if critique.strongest_implementation}
        if len(strongest) == 1:
            return f"Structured review leans toward `{next(iter(strongest))}`. Validate manually before adopting."
        if len(strongest) > 1:
            return f"Reviewers disagreed on the strongest implementation: {', '.join(sorted(strongest))}."
        finding_counts: dict[str, int] = {}
        for critique in critiques:
            finding_counts[critique.subject_provider] = finding_counts.get(critique.subject_provider, 0) + len(critique.findings)
        if not finding_counts:
            return "No findings were parsed. Compare summaries manually before adopting."
        lowest = min(finding_counts.values())
        candidates = sorted(provider for provider, count in finding_counts.items() if count == lowest)
        return f"Fewest parsed findings: {', '.join(candidates)}. Validate manually before adoption."

    def _finalize_critiques(self, cross_review_id: str, critiques: list[CrossReviewCritiqueRecord]) -> None:
        if not critiques:
            return
        disagreement = len({critique.verdict for critique in critiques}) > 1
        human_required = any(
            critique.required_human_inspection
            or critique.verdict == "ESCALATE"
            or self._parse_human_inspection(critique.summary)
            for critique in critiques
        )
        strongest_candidates = [critique.strongest_implementation for critique in critiques if critique.strongest_implementation]
        if not strongest_candidates:
            passing = [critique.subject_provider for critique in critiques if critique.verdict == "PASS"]
            if len(passing) == 1:
                strongest_candidates = passing
        strongest = strongest_candidates[0] if len(strongest_candidates) == 1 else None
        for critique in critiques:
            self.store.upsert_cross_review_critique(
                cross_review_id,
                critique.duel_id,
                critique.reviewer_provider,
                critique.subject_provider,
                critique.status,
                session_id=critique.session_id,
                verdict=critique.verdict,
                strongest_implementation=critique.strongest_implementation or strongest,
                reviewer_disagreement=disagreement,
                required_human_inspection=human_required,
                summary=critique.summary,
                findings=critique.findings,
                artifact_path=critique.artifact_path,
            )

    def _findings(self, summary: str) -> list[str]:
        findings: list[str] = []
        for raw_line in summary.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(("-", "*")):
                findings.append(line.lstrip("-* "))
            elif any(marker in line.lower() for marker in ("finding", "risk", "bug", "regression", "missing", "fail")):
                findings.append(line)
            if len(findings) >= 20:
                break
        if not findings and summary.strip():
            findings.append(summary.strip()[:500])
        return findings

    def _review_analysis(self, summary: str, reviewer_provider: AgentProvider, subject_provider: AgentProvider) -> dict[str, object]:
        verdict = self._parse_verdict(summary)
        strongest = self._parse_strongest(summary, subject_provider)
        required_human_inspection = self._parse_human_inspection(summary) or verdict == "ESCALATE"
        reviewer_disagreement = self._parse_disagreement(summary)
        if not strongest and verdict == "PASS":
            strongest = subject_provider
        return {
            "verdict": verdict,
            "strongest_implementation": strongest,
            "reviewer_disagreement": reviewer_disagreement,
            "required_human_inspection": required_human_inspection,
        }

    def _parse_verdict(self, summary: str) -> str:
        for line in summary.splitlines():
            match = re.match(r"VERDICT:\s*(PASS|FIX|ESCALATE)\b", line.strip(), re.IGNORECASE)
            if match:
                return match.group(1).upper()
        return "ESCALATE"

    def _parse_strongest(self, summary: str, fallback: AgentProvider) -> AgentProvider | None:
        patterns = (
            r"strongest implementation[:\s]+([A-Za-z0-9_.-]+)",
            r"winner[:\s]+([A-Za-z0-9_.-]+)",
            r"recommend(?:ation)?[:\s]+([A-Za-z0-9_.-]+)",
        )
        for pattern in patterns:
            match = re.search(pattern, summary, re.IGNORECASE)
            if match:
                return match.group(1)
        return fallback if self._parse_verdict(summary) == "PASS" else None

    def _parse_disagreement(self, summary: str) -> bool:
        lowered = summary.lower()
        return any(marker in lowered for marker in ("disagree", "disagreement", "conflict", "contrast"))

    def _parse_human_inspection(self, summary: str) -> bool:
        lowered = summary.lower()
        return any(marker in lowered for marker in ("human", "manual", "inspect", "operator"))

    def _git_text(self, workspace: Path, *args: str) -> str:
        process = subprocess.run(["git", *args], cwd=workspace, text=True, capture_output=True, check=False)
        return process.stdout.strip() if process.returncode == 0 else process.stderr.strip()

    def _git_lines(self, workspace: Path, *args: str) -> list[str]:
        return [line for line in self._git_text(workspace, *args).splitlines() if line]

    def _added_file_lines(self, path: Path) -> str:
        try:
            return "".join(f"+{line}" for line in path.read_text(errors="replace").splitlines(keepends=True))
        except OSError as exc:
            return f"+<unable to read file: {exc}>"

    @staticmethod
    def _safe_provider(provider: str) -> str:
        return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in provider)

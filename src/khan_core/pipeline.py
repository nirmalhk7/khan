from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .agent_adapters import DEFAULT_AGENT_REGISTRY, AgentAdapterRegistry
from .agents import AgentSessionRunner
from .config import load_config
from .cross_review import CrossReviewError, CrossReviewRunner
from .duel import DuelRunner
from .models import AgentProvider, CrossReviewCritiqueRecord, DuelParticipantRecord, PipelineRecord, ProjectConfig, TaskCapsule, TaskRecord
from .store import Store


class PipelineError(RuntimeError):
    pass


@dataclass(frozen=True)
class PipelineDecision:
    recommended_provider: AgentProvider | None
    confidence: str
    summary: str
    risks: list[str]


class PipelineRunner:
    def __init__(self, config_path: Path | None = None, registry: AgentAdapterRegistry | None = None) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        self.store = Store(self.config.global_config.state_dir)
        self.registry = registry or DEFAULT_AGENT_REGISTRY

    def run_pipeline(
        self,
        task: TaskRecord,
        *,
        planner_provider: AgentProvider = "codex",
        builder_providers: list[AgentProvider] | None = None,
    ) -> PipelineRecord:
        project = self._project(task.project)
        builders = builder_providers or ["codex", "cursor-agent"]
        pipeline = self.store.create_pipeline(
            task.id,
            project.name,
            task.prompt,
            planner_provider=planner_provider,
            builder_providers=builders,
        )
        try:
            self.store.update_pipeline(pipeline.id, "planning", "Creating implementation brief.")
            brief_path = self._run_planner(pipeline, task, project, planner_provider)

            self.store.update_pipeline(pipeline.id, "building", "Running independent builder duel.")
            duel = DuelRunner(self.config_path, registry=self.registry).run_duel(
                project.name,
                self._builder_prompt(task.prompt, brief_path),
                providers=builders,
                validate=True,
            )
            self.store.upsert_pipeline_phase(
                pipeline.id,
                "build",
                "succeeded" if duel.status != "failed" else "failed",
                duel_id=duel.id,
                artifact_path=duel.report_path,
                summary=duel.summary,
            )

            review_id = None
            review_summary = "Cross-review skipped; fewer than two successful builders."
            critiques: list[CrossReviewCritiqueRecord] = []
            participants = self.store.list_duel_participants(duel.id)
            if len([p for p in participants if p.status == "succeeded" and p.workspace]) >= 2:
                self.store.update_pipeline(pipeline.id, "reviewing", "Running cross-review.")
                try:
                    review = CrossReviewRunner(self.config_path, registry=self.registry).run_cross_review(duel.id)
                    review_id = review.id
                    review_summary = review.summary
                    critiques = self.store.list_cross_review_critiques(review.id)
                    self.store.upsert_pipeline_phase(
                        pipeline.id,
                        "review",
                        "succeeded" if review.status != "failed" else "failed",
                        cross_review_id=review.id,
                        artifact_path=review.report_path,
                        summary=review.summary,
                    )
                except CrossReviewError as exc:
                    review_summary = str(exc)
                    self.store.upsert_pipeline_phase(pipeline.id, "review", "skipped", summary=review_summary)
            else:
                self.store.upsert_pipeline_phase(pipeline.id, "review", "skipped", summary=review_summary)

            decision = self._decide(project, participants, critiques)
            report = self._write_report(pipeline.id, task, project, brief_path, duel.id, review_id, participants, critiques, decision, review_summary)
            self.store.upsert_pipeline_phase(
                pipeline.id,
                "decide",
                "succeeded" if decision.recommended_provider else "failed",
                artifact_path=str(report),
                summary=decision.summary,
            )
            status = "awaiting_decision" if decision.recommended_provider else "failed"
            self.store.update_pipeline(
                pipeline.id,
                status,
                decision.summary,
                recommended_provider=decision.recommended_provider,
                report_path=str(report),
            )
        except Exception as exc:
            self.store.update_pipeline(pipeline.id, "failed", f"{type(exc).__name__}: {exc}")
            if isinstance(exc, PipelineError):
                raise
            raise PipelineError(f"{type(exc).__name__}: {exc}") from exc
        return self.store.get_pipeline(pipeline.id)

    def _run_planner(self, pipeline: PipelineRecord, task: TaskRecord, project: ProjectConfig, provider: AgentProvider) -> Path:
        self.store.upsert_pipeline_phase(pipeline.id, "plan", "running", provider=provider)
        prompt = (
            f"You are planning Khan pipeline {pipeline.id}.\n"
            "Create a concise implementation brief for independent builders. Do not edit files.\n"
            "Include scope, likely files, validation commands, risks, and acceptance checks.\n\n"
            f"Task:\n{task.prompt}\n"
        )
        runner = AgentSessionRunner(self.config_path, registry=self.registry)
        runner.config = self.config
        runner.store = self.store
        session_id = runner.start_session(provider, project.name, prompt, force_worktree=True)
        session = self.store.get_agent_session(session_id)
        content = self._brief_content(pipeline, task, project, session.summary)
        artifact = self.store.write_artifact(pipeline.id, "pipeline-brief.md", content)
        self.store.upsert_pipeline_phase(
            pipeline.id,
            "plan",
            "succeeded" if session.status == "succeeded" else "failed",
            provider=provider,
            session_id=session_id,
            artifact_path=str(artifact),
            summary=session.summary,
        )
        if session.status != "succeeded":
            raise PipelineError(f"Planner {provider} failed: {session.summary}")
        return artifact

    def _brief_content(self, pipeline: PipelineRecord, task: TaskRecord, project: ProjectConfig, summary: str) -> str:
        capsule = self.store.get_task_capsule(task.id)
        lines = [
            "# Pipeline Brief",
            "",
            f"- Pipeline: `{pipeline.id}`",
            f"- Task: `{task.id}`",
            f"- Project: `{project.name}`",
            "",
            "## Objective",
            "",
            task.prompt,
            "",
            "## Planner Brief",
            "",
            summary or "_Planner returned no summary._",
            "",
            "## Verification",
            "",
        ]
        verification = capsule.verification or project.validate_commands
        lines.extend(f"- `{command}`" for command in verification)
        if not verification:
            lines.append("- _No validation commands configured._")
        return "\n".join(lines) + "\n"

    def _builder_prompt(self, prompt: str, brief_path: Path) -> str:
        brief = brief_path.read_text()
        return (
            "Use the implementation brief below as the shared plan, then build independently.\n"
            "Do not adopt another provider's work. Run relevant validation or explain gaps.\n\n"
            "## Original Task\n"
            f"{prompt}\n\n"
            "## Implementation Brief\n"
            f"{brief}\n"
        )

    def _decide(
        self,
        project: ProjectConfig,
        participants: list[DuelParticipantRecord],
        critiques: list[CrossReviewCritiqueRecord],
    ) -> PipelineDecision:
        scores = {participant.provider: self._score(project, participant, critiques) for participant in participants}
        adoptable = [p for p in participants if p.status == "succeeded" and p.validation_ok is not False]
        if not adoptable:
            return PipelineDecision(None, "low", "No builder produced an adoptable candidate.", ["No builder completed without validation failure."])
        best = max(adoptable, key=lambda participant: scores[participant.provider])
        sorted_scores = sorted((scores[p.provider], p.provider) for p in adoptable)
        margin = sorted_scores[-1][0] - (sorted_scores[-2][0] if len(sorted_scores) > 1 else 0)
        risks = list(dict.fromkeys(best.open_risks + self._review_risks(best.provider, critiques) + self._protected_risks(project, best.changed_files)))
        confidence = "high" if best.validation_ok is True and margin >= 20 and not risks else "medium" if best.validation_ok is not False else "low"
        summary = f"Recommend `{best.provider}` with {confidence} confidence (score={scores[best.provider]})."
        return PipelineDecision(best.provider, confidence, summary, risks)

    def _score(self, project: ProjectConfig, participant: DuelParticipantRecord, critiques: list[CrossReviewCritiqueRecord]) -> int:
        score = 0
        if participant.status == "succeeded":
            score += 60
        if participant.validation_ok is True:
            score += 25
        elif participant.validation_ok is False:
            score -= 35
        score -= min(len(participant.changed_files), 20)
        score -= 5 * len(participant.open_risks)
        score -= 20 * len(self._protected_risks(project, participant.changed_files))
        for critique in critiques:
            if critique.subject_provider != participant.provider:
                continue
            if critique.verdict == "PASS":
                score += 10
            elif critique.verdict == "FIX":
                score -= 10
            else:
                score -= 20
            score -= 2 * len(critique.findings)
            if critique.strongest_implementation == participant.provider:
                score += 15
        return score

    def _review_risks(self, provider: AgentProvider, critiques: list[CrossReviewCritiqueRecord]) -> list[str]:
        risks: list[str] = []
        for critique in critiques:
            if critique.subject_provider != provider:
                continue
            if critique.verdict != "PASS":
                risks.append(f"{critique.reviewer_provider} verdict for {provider}: {critique.verdict}.")
            if critique.required_human_inspection:
                risks.append(f"{critique.reviewer_provider} requires human inspection.")
        return risks

    def _protected_risks(self, project: ProjectConfig, changed_files: list[str]) -> list[str]:
        touched = [
            path for path in changed_files
            if any(path == protected or path.startswith(f"{protected.rstrip('/')}/") for protected in project.protected_paths)
        ]
        return [f"Protected path touched: {path}" for path in touched]

    def _write_report(
        self,
        pipeline_id: str,
        task: TaskRecord,
        project: ProjectConfig,
        brief_path: Path,
        duel_id: str,
        review_id: str | None,
        participants: list[DuelParticipantRecord],
        critiques: list[CrossReviewCritiqueRecord],
        decision: PipelineDecision,
        review_summary: str,
    ) -> Path:
        card = self._decision_card(pipeline_id, task, decision, participants, critiques)
        card_path = self.store.write_artifact(pipeline_id, "decision-card.md", card)
        lines = [
            "# Pipeline Report",
            "",
            f"- Pipeline: `{pipeline_id}`",
            f"- Project: `{project.name}`",
            f"- Task: `{task.id}`",
            f"- Brief: `{brief_path}`",
            f"- Duel: `{duel_id}`",
            f"- Cross-review: `{review_id or '-'}`",
            f"- Decision card: `{card_path}`",
            "",
            "## Decision",
            "",
            decision.summary,
            "",
            "## Builders",
            "",
            "| Provider | Status | Validation | Changed Files | Risks | Workspace |",
            "| --- | --- | --- | ---: | ---: | --- |",
        ]
        for participant in participants:
            validation = "skipped" if participant.validation_ok is None else "pass" if participant.validation_ok else "fail"
            lines.append(
                f"| {participant.provider} | {participant.status} | {validation} | {len(participant.changed_files)} | "
                f"{len(participant.open_risks)} | `{participant.workspace or '-'}` |"
            )
        lines.extend(["", "## Cross-Review", "", review_summary or "_No cross-review summary._", ""])
        for critique in critiques:
            lines.extend(
                [
                    f"### {critique.reviewer_provider} on {critique.subject_provider}",
                    "",
                    f"- Verdict: `{critique.verdict}`",
                    f"- Strongest: `{critique.strongest_implementation or '-'}`",
                    f"- Findings: {len(critique.findings)}",
                    "",
                ]
            )
        return self.store.write_artifact(pipeline_id, "pipeline-report.md", "\n".join(lines) + "\n")

    def _decision_card(
        self,
        pipeline_id: str,
        task: TaskRecord,
        decision: PipelineDecision,
        participants: list[DuelParticipantRecord],
        critiques: list[CrossReviewCritiqueRecord],
    ) -> str:
        provider = decision.recommended_provider or "-"
        lines = [
            "# Pipeline Decision Card",
            "",
            f"- Pipeline: `{pipeline_id}`",
            f"- Task: `{task.id}`",
            f"- Recommended provider: `{provider}`",
            f"- Confidence: `{decision.confidence}`",
            f"- Summary: {decision.summary}",
            f"- Primary action: `khan adopt {pipeline_id} --provider {provider}`" if decision.recommended_provider else "- Primary action: `khan reject {pipeline_id}`",
            f"- Secondary action: `khan reject {pipeline_id}`",
            "",
            "## Evidence",
            "",
        ]
        for participant in participants:
            validation = "skipped" if participant.validation_ok is None else "pass" if participant.validation_ok else "fail"
            lines.append(f"- `{participant.provider}` status={participant.status} validation={validation} changed_files={len(participant.changed_files)}")
        for critique in critiques:
            lines.append(f"- `{critique.reviewer_provider}` reviewed `{critique.subject_provider}` verdict={critique.verdict}")
        lines.extend(["", "## Risks", ""])
        lines.extend(f"- {risk}" for risk in decision.risks)
        if not decision.risks:
            lines.append("- _None recorded._")
        return "\n".join(lines) + "\n"

    def _project(self, project_name: str) -> ProjectConfig:
        try:
            return self.config.projects[project_name]
        except KeyError as exc:
            raise PipelineError(f"Project not found in config: {project_name}") from exc

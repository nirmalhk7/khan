from __future__ import annotations

from collections import Counter
from typing import Any

from .models import AgentSessionRecord, CrossReviewRecord, DaemonRecord, DecisionCard, DuelRecord, PipelineRecord, QueueItemRecord, RunRecord
from .store import Store


class AttentionRouter:
    def __init__(self, store: Store) -> None:
        self.store = store

    def cards(self) -> list[DecisionCard]:
        cards: list[DecisionCard] = []
        for run in self.store.list_runs():
            cards.append(self._run_card(run))
        for session in self.store.list_agent_sessions():
            cards.append(self._session_card(session))
        for item in self.store.list_queue_items(limit=200):
            cards.append(self._queue_card(item))
        for daemon in self.store.list_daemons(limit=20):
            cards.append(self._daemon_card(daemon))
        for duel in self.store.list_duels(limit=50):
            cards.append(self._duel_card(duel))
        for pipeline in self.store.list_pipelines(limit=50):
            cards.append(self._pipeline_card(pipeline))
        for cross_review in self.store.list_cross_reviews(limit=50):
            cards.append(self._cross_review_card(cross_review))
        return sorted(cards, key=lambda card: card.score, reverse=True)

    def metrics(self) -> dict[str, Any]:
        runs = self.store.list_runs()
        sessions = self.store.list_agent_sessions()
        queue_items = self.store.list_queue_items(limit=1000)
        run_statuses = Counter(run.status for run in runs)
        session_statuses = Counter(session.status for session in sessions)
        queue_statuses = Counter(item.status for item in queue_items)
        daemons = self.store.list_daemons(limit=100)
        daemon_statuses = Counter(daemon.status for daemon in daemons)
        duels = self.store.list_duels(limit=100)
        duel_statuses = Counter(duel.status for duel in duels)
        adoptions = self.store.list_adoption_decisions(limit=500)
        adoption_statuses = Counter(adoption.status for adoption in adoptions)
        cross_reviews = self.store.list_cross_reviews(limit=100)
        cross_review_statuses = Counter(cross_review.status for cross_review in cross_reviews)
        pipelines = self.store.list_pipelines(limit=100)
        pipeline_statuses = Counter(pipeline.status for pipeline in pipelines)
        decision_cards = self.cards()
        return {
            "runs": {
                "total": len(runs),
                "status_counts": dict(sorted(run_statuses.items())),
                "needs_human": run_statuses.get("needs_human", 0),
                "succeeded": run_statuses.get("succeeded", 0),
                "failed": run_statuses.get("failed", 0),
            },
            "sessions": {
                "total": len(sessions),
                "status_counts": dict(sorted(session_statuses.items())),
                "active": sum(session_statuses.get(status, 0) for status in ("queued", "running", "stopping")),
                "succeeded": session_statuses.get("succeeded", 0),
                "failed": session_statuses.get("failed", 0),
            },
            "attention": {
                "decision_required": sum(1 for card in decision_cards if card.classification == "decision_required"),
                "watch": sum(1 for card in decision_cards if card.classification == "watch"),
                "stopped": sum(1 for card in decision_cards if card.classification == "stopped"),
            },
            "queue": {
                "total": len(queue_items),
                "status_counts": dict(sorted(queue_statuses.items())),
                "queued": queue_statuses.get("queued", 0),
                "running": queue_statuses.get("running", 0),
                "failed": queue_statuses.get("failed", 0),
            },
            "daemons": {
                "total": len(daemons),
                "status_counts": dict(sorted(daemon_statuses.items())),
                "running": daemon_statuses.get("running", 0),
                "failed": daemon_statuses.get("failed", 0),
            },
            "duels": {
                "total": len(duels),
                "status_counts": dict(sorted(duel_statuses.items())),
                "awaiting_decision": duel_statuses.get("awaiting_decision", 0),
                "failed": duel_statuses.get("failed", 0),
            },
            "adoptions": {
                "total": len(adoptions),
                "status_counts": dict(sorted(adoption_statuses.items())),
                "adopted": adoption_statuses.get("adopted", 0),
                "rejected": adoption_statuses.get("rejected", 0),
                "failed": adoption_statuses.get("failed", 0),
            },
            "cross_reviews": {
                "total": len(cross_reviews),
                "status_counts": dict(sorted(cross_review_statuses.items())),
                "awaiting_decision": cross_review_statuses.get("awaiting_decision", 0),
                "failed": cross_review_statuses.get("failed", 0),
            },
            "pipelines": {
                "total": len(pipelines),
                "status_counts": dict(sorted(pipeline_statuses.items())),
                "awaiting_decision": pipeline_statuses.get("awaiting_decision", 0),
                "failed": pipeline_statuses.get("failed", 0),
            },
        }

    def _run_card(self, run: RunRecord) -> DecisionCard:
        if run.status in {"needs_human", "awaiting_decision"}:
            return DecisionCard(
                run_id=run.id,
                subject_type="run",
                classification="decision_required",
                score=100,
                summary=run.summary or f"Run {run.id[:8]} needs input.",
                evidence=[f"status={run.status}", f"project={run.project}", f"iteration={run.iteration}"],
                recommended_actions=[
                    f"khan run status {run.id}",
                    f"khan run logs {run.id}",
                    f"khan run artifacts {run.id}",
                ],
            )
        if run.status in {"retryable_failure", "paused", "stopping"}:
            return DecisionCard(
                run_id=run.id,
                subject_type="run",
                classification="watch",
                score=70,
                summary=run.summary or f"Run {run.id[:8]} needs monitoring.",
                evidence=[f"status={run.status}", f"project={run.project}"],
                recommended_actions=[f"khan watch {run.id}", f"khan run logs {run.id}"],
            )
        if run.status in {"failed", "cancelled"}:
            return DecisionCard(
                run_id=run.id,
                subject_type="run",
                classification="stopped",
                score=50,
                summary=run.summary or f"Run {run.id[:8]} stopped.",
                evidence=[f"status={run.status}", f"project={run.project}"],
                recommended_actions=[f"khan run logs {run.id}", f"khan task retry {run.id}"],
            )
        return DecisionCard(
            run_id=run.id,
            subject_type="run",
            classification="healthy",
            score=10,
            summary=run.summary or f"Run {run.id[:8]} is {run.status}.",
            evidence=[f"status={run.status}", f"project={run.project}"],
            recommended_actions=[f"khan run status {run.id}"],
        )

    def _session_card(self, session: AgentSessionRecord) -> DecisionCard:
        if session.status in {"running", "queued", "stopping"}:
            return DecisionCard(
                run_id=session.id,
                subject_type="session",
                classification="watch",
                score=60,
                summary=session.summary or f"Session {session.id[:8]} is {session.status}.",
                evidence=[f"status={session.status}", f"provider={session.provider}", f"project={session.project}"],
                recommended_actions=[f"khan session status {session.id}", f"khan session logs {session.id}"],
            )
        if session.status in {"failed", "cancelled"}:
            return DecisionCard(
                run_id=session.id,
                subject_type="session",
                classification="stopped",
                score=45,
                summary=session.summary or f"Session {session.id[:8]} stopped.",
                evidence=[f"status={session.status}", f"provider={session.provider}", f"project={session.project}"],
                recommended_actions=[f"khan session logs {session.id}"],
            )
        return DecisionCard(
            run_id=session.id,
            subject_type="session",
            classification="healthy",
            score=5,
            summary=session.summary or f"Session {session.id[:8]} is {session.status}.",
            evidence=[f"status={session.status}", f"provider={session.provider}", f"project={session.project}"],
            recommended_actions=[f"khan session status {session.id}"],
        )

    def _queue_card(self, item: QueueItemRecord) -> DecisionCard:
        if item.status == "failed":
            return DecisionCard(
                run_id=item.id,
                subject_type="queue",
                classification="stopped",
                score=55,
                summary=item.error or f"Queue item {item.id[:8]} failed.",
                evidence=[f"kind={item.kind}", f"status={item.status}", f"attempts={item.attempts}"],
                recommended_actions=[f"khan queue requeue {item.id}", f"khan queue list --status failed"],
            )
        if item.status in {"queued", "running"}:
            return DecisionCard(
                run_id=item.id,
                subject_type="queue",
                classification="watch",
                score=40 if item.status == "queued" else 65,
                summary=f"Queue item {item.id[:8]} is {item.status}.",
                evidence=[f"kind={item.kind}", f"status={item.status}", f"attempts={item.attempts}"],
                recommended_actions=["khan queue work --once", f"khan queue list --status {item.status}"],
            )
        return DecisionCard(
            run_id=item.id,
            subject_type="queue",
            classification="healthy",
            score=1,
            summary=f"Queue item {item.id[:8]} is {item.status}.",
            evidence=[f"kind={item.kind}", f"status={item.status}", f"attempts={item.attempts}"],
            recommended_actions=[f"khan queue list --status {item.status}"],
        )

    def _daemon_card(self, daemon: DaemonRecord) -> DecisionCard:
        if daemon.status == "failed":
            return DecisionCard(
                run_id=daemon.id,
                subject_type="daemon",
                classification="stopped",
                score=80,
                summary=daemon.error or f"Daemon {daemon.id[:8]} failed.",
                evidence=[f"pid={daemon.pid}", f"status={daemon.status}"],
                recommended_actions=["khan daemon status", "khan daemon start"],
            )
        if daemon.status in {"running", "stopping"}:
            return DecisionCard(
                run_id=daemon.id,
                subject_type="daemon",
                classification="watch",
                score=35 if daemon.status == "running" else 65,
                summary=f"Daemon {daemon.id[:8]} is {daemon.status}.",
                evidence=[f"pid={daemon.pid}", f"heartbeat={daemon.heartbeat_at.isoformat()}"],
                recommended_actions=["khan daemon status", f"khan daemon stop --daemon-id {daemon.id}"],
            )
        return DecisionCard(
            run_id=daemon.id,
            subject_type="daemon",
            classification="healthy",
            score=1,
            summary=f"Daemon {daemon.id[:8]} is {daemon.status}.",
            evidence=[f"pid={daemon.pid}", f"status={daemon.status}"],
            recommended_actions=["khan daemon status"],
        )

    def _duel_card(self, duel: DuelRecord) -> DecisionCard:
        if duel.status == "awaiting_decision":
            return DecisionCard(
                run_id=duel.id,
                subject_type="duel",
                classification="decision_required",
                score=88,
                summary=duel.summary or f"Duel {duel.id[:8]} is ready for provider selection.",
                evidence=[f"project={duel.project}", f"providers={', '.join(duel.providers)}"],
                recommended_actions=[f"khan duel show {duel.id}", f"khan duel artifacts {duel.id}"],
            )
        if duel.status == "running":
            return DecisionCard(
                run_id=duel.id,
                subject_type="duel",
                classification="watch",
                score=55,
                summary=duel.summary or f"Duel {duel.id[:8]} is running.",
                evidence=[f"project={duel.project}", f"providers={', '.join(duel.providers)}"],
                recommended_actions=[f"khan duel show {duel.id}"],
            )
        if duel.status == "failed":
            return DecisionCard(
                run_id=duel.id,
                subject_type="duel",
                classification="stopped",
                score=75,
                summary=duel.summary or f"Duel {duel.id[:8]} failed.",
                evidence=[f"project={duel.project}", f"providers={', '.join(duel.providers)}"],
                recommended_actions=[f"khan duel show {duel.id}", f"khan duel artifacts {duel.id}"],
            )
        return DecisionCard(
            run_id=duel.id,
            subject_type="duel",
            classification="healthy",
            score=1,
            summary=duel.summary or f"Duel {duel.id[:8]} is {duel.status}.",
            evidence=[f"project={duel.project}", f"status={duel.status}"],
            recommended_actions=[f"khan duel show {duel.id}"],
        )

    def _cross_review_card(self, cross_review: CrossReviewRecord) -> DecisionCard:
        if cross_review.status == "awaiting_decision":
            return DecisionCard(
                run_id=cross_review.id,
                subject_type="cross_review",
                classification="decision_required",
                score=90,
                summary=cross_review.summary or f"Cross-review {cross_review.id[:8]} is ready.",
                evidence=[f"duel={cross_review.duel_id}"],
                recommended_actions=[
                    f"khan cross-review-show {cross_review.id}",
                    f"khan cross-review-artifacts {cross_review.id}",
                ],
            )
        if cross_review.status == "running":
            return DecisionCard(
                run_id=cross_review.id,
                subject_type="cross_review",
                classification="watch",
                score=60,
                summary=cross_review.summary or f"Cross-review {cross_review.id[:8]} is running.",
                evidence=[f"duel={cross_review.duel_id}"],
                recommended_actions=[f"khan cross-review-show {cross_review.id}"],
            )
        if cross_review.status == "failed":
            return DecisionCard(
                run_id=cross_review.id,
                subject_type="cross_review",
                classification="stopped",
                score=78,
                summary=cross_review.summary or f"Cross-review {cross_review.id[:8]} failed.",
                evidence=[f"duel={cross_review.duel_id}"],
                recommended_actions=[
                    f"khan cross-review-show {cross_review.id}",
                    f"khan cross-review-artifacts {cross_review.id}",
                ],
            )
        return DecisionCard(
            run_id=cross_review.id,
            subject_type="cross_review",
            classification="healthy",
            score=1,
            summary=cross_review.summary or f"Cross-review {cross_review.id[:8]} is {cross_review.status}.",
            evidence=[f"duel={cross_review.duel_id}", f"status={cross_review.status}"],
            recommended_actions=[f"khan cross-review-show {cross_review.id}"],
        )

    def _pipeline_card(self, pipeline: PipelineRecord) -> DecisionCard:
        phases = self.store.list_pipeline_phases(pipeline.id)
        risks = [
            line.removeprefix("- ").strip()
            for phase in phases
            for line in phase.summary.splitlines()
            if "risk" in line.lower()
        ][:5]
        if pipeline.status == "awaiting_decision":
            action = (
                f"khan adopt {pipeline.id} --provider {pipeline.recommended_provider}"
                if pipeline.recommended_provider
                else f"khan show {pipeline.id}"
            )
            return DecisionCard(
                run_id=pipeline.id,
                subject_type="pipeline",
                classification="decision_required",
                score=100,
                summary=pipeline.decision_summary or f"Pipeline {pipeline.id[:8]} is awaiting adoption decision.",
                evidence=[f"project={pipeline.project}", f"recommended={pipeline.recommended_provider or '-'}"],
                recommended_actions=[action, f"khan show {pipeline.id}", f"khan reject {pipeline.id}"],
                recommended_provider=pipeline.recommended_provider,
                confidence=self._confidence_from_summary(pipeline.decision_summary),
                primary_action=action,
                secondary_actions=[f"khan show {pipeline.id}", f"khan reject {pipeline.id}"],
                risks=risks,
            )
        if pipeline.status in {"queued", "planning", "building", "reviewing"}:
            return DecisionCard(
                run_id=pipeline.id,
                subject_type="pipeline",
                classification="watch",
                score=70,
                summary=pipeline.decision_summary or f"Pipeline {pipeline.id[:8]} is {pipeline.status}.",
                evidence=[f"project={pipeline.project}", f"status={pipeline.status}"],
                recommended_actions=[f"khan show {pipeline.id}"],
            )
        if pipeline.status == "failed":
            return DecisionCard(
                run_id=pipeline.id,
                subject_type="pipeline",
                classification="stopped",
                score=85,
                summary=pipeline.decision_summary or f"Pipeline {pipeline.id[:8]} failed.",
                evidence=[f"project={pipeline.project}", f"status={pipeline.status}"],
                recommended_actions=[f"khan show {pipeline.id}"],
                risks=risks,
            )
        return DecisionCard(
            run_id=pipeline.id,
            subject_type="pipeline",
            classification="healthy",
            score=1,
            summary=pipeline.decision_summary or f"Pipeline {pipeline.id[:8]} is {pipeline.status}.",
            evidence=[f"project={pipeline.project}", f"status={pipeline.status}"],
            recommended_actions=[f"khan show {pipeline.id}"],
        )

    def _confidence_from_summary(self, summary: str) -> str | None:
        lowered = summary.lower()
        if "high confidence" in lowered:
            return "high"
        if "medium confidence" in lowered:
            return "medium"
        if "low confidence" in lowered:
            return "low"
        return None

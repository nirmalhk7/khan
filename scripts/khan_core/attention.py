from __future__ import annotations

from collections import Counter
from typing import Any

from .models import AgentSessionRecord, DaemonRecord, DecisionCard, QueueItemRecord, RunRecord
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

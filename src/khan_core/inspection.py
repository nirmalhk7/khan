from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import load_config
from .models import AdoptionRecord, AgentSessionRecord, CrossReviewRecord, DaemonRecord, DuelRecord, PipelineRecord, QueueItemRecord, RunRecord, TaskRecord
from .store import Store


class InspectError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecordRef:
    kind: str
    id: str
    status: str
    project: str
    summary: str
    updated_at: datetime
    record: Any


class Inspector:
    def __init__(self, config_path: Path | None = None) -> None:
        self.config = load_config(config_path)
        self.store = Store(self.config.global_config.state_dir)

    def resolve(self, selector: str) -> RecordRef:
        matches = [ref for ref in self._all_refs() if ref.id == selector or ref.id.startswith(selector)]
        exact = [ref for ref in matches if ref.id == selector]
        if len(exact) == 1:
            return exact[0]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise InspectError(f"No Khan record found for id prefix: {selector}")
        raise InspectError(
            f"Ambiguous id prefix {selector!r}; matches: {', '.join(f'{ref.kind}:{ref.id[:8]}' for ref in matches[:8])}"
        )

    def last(self, kind: str = "any") -> RecordRef:
        normalized = self._normalize_kind(kind)
        refs = [ref for ref in self._all_refs() if normalized == "any" or ref.kind == normalized]
        if not refs:
            raise InspectError(f"No Khan records found for type: {kind}")
        return sorted(refs, key=lambda ref: ref.updated_at, reverse=True)[0]

    def active_rows(self) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for run in self.store.list_runs(active_only=True):
            rows.append(
                {
                    "type": "run",
                    "id": run.id,
                    "project": run.project,
                    "status": run.status,
                    "summary": run.summary or f"Task {run.task_id[:8]}",
                    "next": self._run_next_action(run),
                }
            )
        for session in self.store.list_agent_sessions(active_only=True):
            rows.append(
                {
                    "type": "session",
                    "id": session.id,
                    "project": session.project,
                    "status": session.status,
                    "summary": session.summary or session.prompt[:80],
                    "next": "khan session logs" if session.status == "running" else "khan session status",
                }
            )
        for item in self.store.list_queue_items(limit=200):
            if item.status not in {"queued", "running"}:
                continue
            rows.append(
                {
                    "type": "queue",
                    "id": item.id,
                    "project": str(item.payload.get("project") or item.payload.get("task_id") or "-"),
                    "status": item.status,
                    "summary": f"{item.kind} priority={item.priority}",
                    "next": "khan queue work --once" if item.status == "queued" else "khan queue list",
                }
            )
        for daemon in self.store.list_daemons(active_only=True, limit=50):
            rows.append(
                {
                    "type": "daemon",
                    "id": daemon.id,
                    "project": "-",
                    "status": daemon.status,
                    "summary": f"pid={daemon.pid} heartbeat={daemon.heartbeat_at.isoformat()}",
                    "next": "khan daemon status",
                }
            )
        for duel in self.store.list_duels(active_only=True, limit=100):
            rows.append(
                {
                    "type": "duel",
                    "id": duel.id,
                    "project": duel.project,
                    "status": duel.status,
                    "summary": duel.summary or duel.prompt[:80],
                    "next": "khan duel show" if duel.status == "running" else "khan adopt/reject",
                }
            )
        for pipeline in self.store.list_pipelines(active_only=True, limit=100):
            rows.append(
                {
                    "type": "pipeline",
                    "id": pipeline.id,
                    "project": pipeline.project,
                    "status": pipeline.status,
                    "summary": pipeline.decision_summary or pipeline.prompt[:80],
                    "next": "khan adopt/reject" if pipeline.status == "awaiting_decision" else "khan show",
                }
            )
        for review in self.store.list_cross_reviews(active_only=True, limit=100):
            rows.append(
                {
                    "type": "cross_review",
                    "id": review.id,
                    "project": review.duel_id,
                    "status": review.status,
                    "summary": review.summary or f"Duel {review.duel_id[:8]}",
                    "next": "khan cross-review-show" if review.status == "running" else "khan adopt/reject",
                }
            )
        return rows

    def summary_markdown(self, selector: str) -> str:
        ref = self.resolve(selector)
        lines = [
            f"# {ref.kind.replace('_', ' ').title()} {ref.id}",
            "",
            f"- Status: {ref.status}",
            f"- Project: {ref.project}",
            f"- Updated: {ref.updated_at.isoformat()}",
            f"- Summary: {ref.summary or '-'}",
        ]
        payload = self._payload(ref)
        if payload:
            lines.extend(["", "## Record", "", "```json", json.dumps(payload, indent=2, sort_keys=True), "```"])
        return "\n".join(lines) + "\n"

    def evidence_markdown(self, selector: str, *, provider: str | None = None) -> str:
        ref = self.resolve(selector)
        payload = self._payload(ref)
        lines = [
            "# Evidence Ledger",
            "",
            f"- Type: {ref.kind}",
            f"- ID: `{ref.id}`",
            f"- Status: {ref.status}",
            f"- Project: {ref.project}",
            f"- Updated: {ref.updated_at.isoformat()}",
            f"- Summary: {ref.summary or '-'}",
        ]
        if ref.kind == "run":
            self._append_run_evidence(lines, ref, payload)
        elif ref.kind == "session":
            self._append_session_evidence(lines, ref, payload)
        elif ref.kind == "duel":
            self._append_duel_evidence(lines, ref, payload, provider=provider)
        elif ref.kind == "pipeline":
            self._append_pipeline_evidence(lines, ref, payload)
        elif ref.kind == "cross_review":
            self._append_cross_review_evidence(lines, ref, payload)
        elif ref.kind == "adoption":
            self._append_adoption_evidence(lines, ref, payload)
        else:
            lines.extend(["", "## Record", "", "```json", json.dumps(payload, indent=2, sort_keys=True), "```"])
        related = payload.get("related_artifacts") or []
        if related:
            lines.extend(["", "## Related Artifacts", ""])
            lines.extend(f"- `{path}`" for path in related)
        return "\n".join(lines) + "\n"

    def evidence_payload(self, selector: str, *, provider: str | None = None) -> dict[str, Any]:
        ref = self.resolve(selector)
        payload = self._payload(ref)
        workspace = None
        if ref.kind in {"run", "session", "adoption", "duel"}:
            try:
                workspace = str(self._workspace_for(ref, provider=provider))
            except InspectError:
                workspace = None
        diff_stat = None
        if workspace:
            try:
                diff_stat = self.diff_text(ref.id, provider=provider, stat=True)
            except InspectError:
                diff_stat = None
        return {
            "type": ref.kind,
            "id": ref.id,
            "status": ref.status,
            "project": ref.project,
            "summary": ref.summary,
            "updated_at": ref.updated_at.isoformat(),
            "workspace": workspace,
            "diff_stat": diff_stat,
            "related_artifacts": self._related_artifacts(ref, payload),
            "record": payload,
        }

    def diff_text(self, selector: str, *, provider: str | None = None, stat: bool = False) -> str:
        ref = self.resolve(selector)
        workspace = self._workspace_for(ref, provider=provider)
        if not workspace.exists():
            raise InspectError(f"Workspace does not exist: {workspace}")
        args = ["git", "diff", "--stat" if stat else "--patch", "HEAD", "--"]
        tracked = subprocess.run(args, cwd=workspace, text=True, capture_output=True, check=False)
        if tracked.returncode not in {0}:
            raise InspectError(tracked.stderr.strip() or tracked.stdout.strip() or "git diff failed")
        untracked = self._untracked_diff(workspace, stat=stat)
        pieces = [tracked.stdout.strip(), untracked.strip()]
        text = "\n".join(piece for piece in pieces if piece)
        return text + ("\n" if text else "No diff.\n")

    def _all_refs(self) -> list[RecordRef]:
        refs: list[RecordRef] = []
        refs.extend(self._task_ref(task) for task in self.store.list_tasks())
        refs.extend(self._run_ref(run) for run in self.store.list_runs())
        refs.extend(self._session_ref(session) for session in self.store.list_agent_sessions())
        refs.extend(self._queue_ref(item) for item in self.store.list_queue_items(limit=500))
        refs.extend(self._daemon_ref(daemon) for daemon in self.store.list_daemons(limit=100))
        refs.extend(self._duel_ref(duel) for duel in self.store.list_duels(limit=500))
        refs.extend(self._pipeline_ref(pipeline) for pipeline in self.store.list_pipelines(limit=500))
        refs.extend(self._cross_review_ref(review) for review in self.store.list_cross_reviews(limit=500))
        refs.extend(self._adoption_ref(decision) for decision in self.store.list_adoption_decisions(limit=500))
        return refs

    def _payload(self, ref: RecordRef) -> dict[str, Any]:
        if ref.kind == "task":
            task: TaskRecord = ref.record
            return {
                "task": task.model_dump(mode="json"),
                "capsule": self.store.get_task_capsule(task.id).model_dump(mode="json"),
            }
        if ref.kind == "run":
            run: RunRecord = ref.record
            return {
                "run": run.model_dump(mode="json"),
                "task": self.store.get_task(run.task_id).model_dump(mode="json"),
                "capsule": self.store.get_task_capsule(run.task_id).model_dump(mode="json"),
                "events": [event.model_dump(mode="json") for event in self.store.list_events(run.id, limit=50)],
                "artifacts": [str(path) for path in self.store.list_artifacts(run.id)],
            }
        if ref.kind == "session":
            session: AgentSessionRecord = ref.record
            return {
                "session": session.model_dump(mode="json"),
                "events": [event.model_dump(mode="json") for event in self.store.list_agent_session_events(session.id, limit=50)],
            }
        if ref.kind == "queue":
            item: QueueItemRecord = ref.record
            return item.model_dump(mode="json")
        if ref.kind == "daemon":
            daemon: DaemonRecord = ref.record
            return daemon.model_dump(mode="json")
        if ref.kind == "duel":
            duel: DuelRecord = ref.record
            return {
                "duel": duel.model_dump(mode="json"),
                "participants": [participant.model_dump(mode="json") for participant in self.store.list_duel_participants(duel.id)],
                "artifacts": [str(path) for path in self.store.list_artifacts(duel.id)],
            }
        if ref.kind == "pipeline":
            pipeline: PipelineRecord = ref.record
            return {
                "pipeline": pipeline.model_dump(mode="json"),
                "task": self.store.get_task(pipeline.task_id).model_dump(mode="json"),
                "phases": [phase.model_dump(mode="json") for phase in self.store.list_pipeline_phases(pipeline.id)],
                "artifacts": [str(path) for path in self.store.list_artifacts(pipeline.id)],
            }
        if ref.kind == "cross_review":
            review: CrossReviewRecord = ref.record
            return {
                "cross_review": review.model_dump(mode="json"),
                "critiques": [critique.model_dump(mode="json") for critique in self.store.list_cross_review_critiques(review.id)],
                "artifacts": [str(path) for path in self.store.list_artifacts(review.id)],
            }
        if ref.kind == "adoption":
            decision: AdoptionRecord = ref.record
            return decision.model_dump(mode="json")
        return {}

    def _related_artifacts(self, ref: RecordRef, payload: dict[str, Any]) -> list[str]:
        related: list[str] = []
        artifacts = payload.get("artifacts", [])
        if isinstance(artifacts, list):
            for path in artifacts:
                if not isinstance(path, str):
                    continue
                name = Path(path).name
                if name.startswith(("relay-", "replay-", "steer-")) or name.endswith("-result.md"):
                    related.append(path)
                if ref.kind == "duel" and name == "duel-report.md":
                    related.append(path)
                if ref.kind == "pipeline" and name in {"pipeline-report.md", "decision-card.md", "pipeline-brief.md"}:
                    related.append(path)
                if ref.kind == "cross_review" and name.startswith("cross-review-"):
                    related.append(path)
        if ref.kind == "session":
            session = payload.get("session")
            if isinstance(session, dict) and session.get("parent_session_id"):
                related.append(f"parent-session:{session['parent_session_id']}")
        return sorted(dict.fromkeys(related))

    def _workspace_for(self, ref: RecordRef, *, provider: str | None = None) -> Path:
        if ref.kind == "run":
            return Path(ref.record.workspace)
        if ref.kind == "session":
            return Path(ref.record.workspace)
        if ref.kind == "adoption":
            return Path(ref.record.source_workspace)
        if ref.kind == "duel":
            participants = self.store.list_duel_participants(ref.id)
            if provider:
                participants = [participant for participant in participants if participant.provider == provider]
            if len(participants) != 1:
                raise InspectError("Duel requires --provider for diff inspection unless it has one participant.")
            return Path(participants[0].workspace)
        raise InspectError(f"{ref.kind} does not have an inspectable workspace.")

    def _append_run_evidence(self, lines: list[str], ref: RecordRef, payload: dict[str, Any]) -> None:
        task = payload["task"]
        capsule = payload["capsule"]
        lines.extend(
            [
                "",
                "## Objective",
                "",
                task["prompt"],
                "",
                "## Capsule",
                "",
                "```json",
                json.dumps(capsule, indent=2, sort_keys=True),
                "```",
                "",
                "## Workspace Diff",
                "",
                self.diff_text(ref.id, stat=True).rstrip() or "No diff.",
                "",
                "## Recent Events",
                "",
            ]
        )
        for event in payload.get("events", []):
            lines.append(f"- {event['ts']} [{event['phase']}] {event['message']}")
        if not payload.get("events"):
            lines.append("- _No events recorded._")
        lines.extend(["", "## Artifacts", ""])
        artifacts = payload.get("artifacts", [])
        for artifact in artifacts:
            lines.append(f"- `{artifact}`")
        if not artifacts:
            lines.append("- _None recorded._")

    def _append_session_evidence(self, lines: list[str], ref: RecordRef, payload: dict[str, Any]) -> None:
        session = payload["session"]
        lines.extend(
            [
                "",
                "## Prompt",
                "",
                session["prompt"],
                "",
                "## External Session",
                "",
                f"- Provider: {session['provider']}",
                f"- External ID: {session.get('external_id') or '-'}",
                f"- Workspace: `{session['workspace']}`",
                "",
                "## Diff",
                "",
                self.diff_text(ref.id, stat=True).rstrip() or "No diff.",
                "",
                "## Recent Events",
                "",
            ]
        )
        for event in payload.get("events", []):
            lines.append(f"- {event['ts']} [{event['stream']}] {event['message']}")
        if not payload.get("events"):
            lines.append("- _No events recorded._")

    def _append_duel_evidence(self, lines: list[str], ref: RecordRef, payload: dict[str, Any], *, provider: str | None) -> None:
        duel = payload["duel"]
        lines.extend(["", "## Prompt", "", duel["prompt"], "", "## Comparison", ""])
        participants = payload.get("participants", [])
        selected = [
            participant
            for participant in participants
            if not provider or participant.get("provider") == provider
        ]
        codex = next((participant for participant in selected if participant.get("provider") == "codex"), None)
        cursor = next(
            (participant for participant in selected if participant.get("provider") == "cursor-agent"),
            None,
        )
        lines.extend(["| Field | Codex | Cursor Agent |", "| --- | --- | --- |"])
        for label, key in (
            ("Status", "status"),
            ("Workspace", "workspace"),
            ("Validation", "validation_summary"),
            ("Artifact", "artifact_path"),
            ("Changed Files", "changed_files"),
            ("Open Risks", "open_risks"),
            ("Summary", "summary"),
        ):
            lines.append(
                f"| {label} | {self._duel_cell(codex, key)} | {self._duel_cell(cursor, key)} |"
            )
        if codex and codex.get("diff_stat") or cursor and cursor.get("diff_stat"):
            lines.extend(["", "## Diff Stat", ""])
            if codex and codex.get("diff_stat"):
                lines.extend(["### Codex", "", "```text", codex["diff_stat"], "```", ""])
            if cursor and cursor.get("diff_stat"):
                lines.extend(["### Cursor Agent", "", "```text", cursor["diff_stat"], "```", ""])
        lines.extend(["## Artifacts", ""])
        artifacts = payload.get("artifacts", [])
        lines.extend(f"- `{artifact}`" for artifact in artifacts)
        if not artifacts:
            lines.append("- _None recorded._")

    def _duel_cell(self, participant: dict[str, Any] | None, key: str) -> str:
        if participant is None:
            return "-"
        value = participant.get(key)
        if key in {"changed_files", "open_risks"}:
            items = value or []
            return ", ".join(str(item) for item in items) if items else "-"
        if value in (None, ""):
            return "-"
        return str(value)

    def _append_cross_review_evidence(self, lines: list[str], ref: RecordRef, payload: dict[str, Any]) -> None:
        review = payload["cross_review"]
        lines.extend(["", "## Duel", "", f"`{review['duel_id']}`", "", "## Critiques", ""])
        for critique in payload.get("critiques", []):
            lines.extend(
                [
                    f"### {critique['reviewer_provider']} reviews {critique['subject_provider']}",
                    "",
                    f"- Status: {critique['status']}",
                    f"- Artifact: `{critique.get('artifact_path') or '-'}`",
                    "",
                    critique.get("summary") or "_No summary recorded._",
                    "",
                ]
            )
            findings = critique.get("findings") or []
            if findings:
                lines.append("Findings:")
                lines.extend(f"- {finding}" for finding in findings)
                lines.append("")
        lines.extend(["## Artifacts", ""])
        artifacts = payload.get("artifacts", [])
        lines.extend(f"- `{artifact}`" for artifact in artifacts)
        if not artifacts:
            lines.append("- _None recorded._")

    def _append_pipeline_evidence(self, lines: list[str], ref: RecordRef, payload: dict[str, Any]) -> None:
        pipeline = payload["pipeline"]
        task = payload["task"]
        lines.extend(
            [
                "",
                "## Decision",
                "",
                pipeline.get("decision_summary") or "_No decision summary recorded._",
                "",
                f"- Recommended provider: `{pipeline.get('recommended_provider') or '-'}`",
                f"- Report: `{pipeline.get('report_path') or '-'}`",
                "",
                "## Task",
                "",
                task["prompt"],
                "",
                "## Phases",
                "",
                "| Phase | Provider | Status | Session | Duel | Cross-Review | Artifact |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for phase in payload.get("phases", []):
            lines.append(
                f"| {phase.get('phase')} | {phase.get('provider') or '-'} | {phase.get('status')} | "
                f"{(phase.get('session_id') or '-')[:8]} | {(phase.get('duel_id') or '-')[:8]} | "
                f"{(phase.get('cross_review_id') or '-')[:8]} | `{phase.get('artifact_path') or '-'}` |"
            )
        artifacts = payload.get("artifacts", [])
        decision_cards = [artifact for artifact in artifacts if Path(artifact).name == "decision-card.md"]
        if decision_cards:
            lines.extend(["", "## Decision Card", ""])
            try:
                lines.append(Path(decision_cards[0]).read_text())
            except OSError:
                lines.append(f"`{decision_cards[0]}`")
        lines.extend(["", "## Artifacts", ""])
        lines.extend(f"- `{artifact}`" for artifact in artifacts)
        if not artifacts:
            lines.append("- _None recorded._")

    def _append_adoption_evidence(self, lines: list[str], ref: RecordRef, payload: dict[str, Any]) -> None:
        lines.extend(
            [
                "",
                "## Decision",
                "",
                f"- Target: {payload.get('target_type')} `{payload.get('target_id')}`",
                f"- Source: `{payload.get('source_workspace')}`",
                f"- Destination: `{payload.get('destination_workspace')}`",
                "",
                "Changed files:",
            ]
        )
        changed = payload.get("changed_files") or []
        lines.extend(f"- `{path}`" for path in changed)
        if not changed:
            lines.append("- _None recorded._")

    def _task_ref(self, task: TaskRecord) -> RecordRef:
        return RecordRef("task", task.id, "created", task.project, task.title, task.created_at, task)

    def _run_ref(self, run: RunRecord) -> RecordRef:
        return RecordRef("run", run.id, run.status, run.project, run.summary, run.updated_at, run)

    def _session_ref(self, session: AgentSessionRecord) -> RecordRef:
        return RecordRef("session", session.id, session.status, session.project, session.summary, session.updated_at, session)

    def _queue_ref(self, item: QueueItemRecord) -> RecordRef:
        return RecordRef("queue", item.id, item.status, str(item.payload.get("project") or item.payload.get("task_id") or "-"), item.error or item.kind, item.updated_at, item)

    def _daemon_ref(self, daemon: DaemonRecord) -> RecordRef:
        return RecordRef("daemon", daemon.id, daemon.status, "-", daemon.error or f"pid={daemon.pid}", daemon.heartbeat_at, daemon)

    def _duel_ref(self, duel: DuelRecord) -> RecordRef:
        return RecordRef("duel", duel.id, duel.status, duel.project, duel.summary, duel.updated_at, duel)

    def _pipeline_ref(self, pipeline: PipelineRecord) -> RecordRef:
        return RecordRef("pipeline", pipeline.id, pipeline.status, pipeline.project, pipeline.decision_summary, pipeline.updated_at, pipeline)

    def _cross_review_ref(self, review: CrossReviewRecord) -> RecordRef:
        return RecordRef("cross_review", review.id, review.status, review.duel_id, review.summary, review.updated_at, review)

    def _adoption_ref(self, decision: AdoptionRecord) -> RecordRef:
        return RecordRef("adoption", decision.id, decision.status, decision.project, decision.summary or decision.error, decision.created_at, decision)

    def _normalize_kind(self, kind: str) -> str:
        normalized = kind.strip().lower().replace("-", "_")
        aliases = {
            "any": "any",
            "task": "task",
            "tasks": "task",
            "run": "run",
            "runs": "run",
            "session": "session",
            "sessions": "session",
            "queue": "queue",
            "daemon": "daemon",
            "duel": "duel",
            "pipeline": "pipeline",
            "pipelines": "pipeline",
            "cross_review": "cross_review",
            "adoption": "adoption",
        }
        if normalized not in aliases:
            raise InspectError("type must be one of: any, task, run, session, queue, daemon, duel, pipeline, cross-review, adoption")
        return aliases[normalized]

    def _run_next_action(self, run: RunRecord) -> str:
        if run.status in {"needs_human", "awaiting_decision"}:
            return f"khan run status {run.id}"
        if run.status in {"paused", "stopping"}:
            return f"khan run resume {run.id}"
        return f"khan watch {run.id}"

    def _untracked_diff(self, workspace: Path, *, stat: bool) -> str:
        listed = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=workspace,
            text=True,
            capture_output=True,
            check=False,
        )
        if listed.returncode != 0:
            raise InspectError(listed.stderr.strip() or "git ls-files failed")
        chunks: list[str] = []
        for rel_path in [path for path in listed.stdout.split("\0") if path]:
            if stat:
                args = ["git", "diff", "--no-index", "--stat", "--", "/dev/null", rel_path]
            else:
                args = ["git", "diff", "--no-index", "--", "/dev/null", rel_path]
            process = subprocess.run(args, cwd=workspace, text=True, capture_output=True, check=False)
            if process.returncode not in {0, 1}:
                raise InspectError(process.stderr.strip() or process.stdout.strip() or "git diff failed")
            if process.stdout.strip():
                chunks.append(process.stdout)
        return "\n".join(chunks)

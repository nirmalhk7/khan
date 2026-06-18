from __future__ import annotations

import fcntl
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator

from .models import (
    AgentProvider,
    AgentSessionEvent,
    AgentSessionRecord,
    AgentSessionStatus,
    AdoptionRecord,
    AdoptionStatus,
    AdoptionTargetType,
    CrossReviewCritiqueRecord,
    CrossReviewCritiqueStatus,
    CrossReviewRecord,
    CrossReviewStatus,
    DaemonRecord,
    DaemonStatus,
    DuelParticipantRecord,
    DuelParticipantStatus,
    DuelRecord,
    DuelStatus,
    EventRecord,
    QueueItemKind,
    QueueItemRecord,
    QueueItemStatus,
    RunCommand,
    RunProcess,
    RunRecord,
    RunStatus,
    TaskCapsule,
    TaskRecord,
)


MIGRATIONS = [
    """
    CREATE TABLE tasks (
      id TEXT PRIMARY KEY, project TEXT NOT NULL, title TEXT NOT NULL,
      prompt TEXT NOT NULL, success_criteria TEXT NOT NULL, profile TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE runs (
      id TEXT PRIMARY KEY, task_id TEXT NOT NULL, project TEXT NOT NULL, status TEXT NOT NULL,
      iteration INTEGER NOT NULL, workspace TEXT NOT NULL, summary TEXT NOT NULL,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, ts TEXT NOT NULL,
      phase TEXT NOT NULL, message TEXT NOT NULL, payload TEXT NOT NULL
    );
    """,
    """
    ALTER TABLE runs ADD COLUMN session_id TEXT;
    ALTER TABLE runs ADD COLUMN process_id INTEGER;
    ALTER TABLE runs ADD COLUMN heartbeat_at TEXT;
    ALTER TABLE runs ADD COLUMN failure_fingerprint TEXT;
    ALTER TABLE runs ADD COLUMN repeated_failures INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE run_processes (
      run_id TEXT NOT NULL, pid INTEGER NOT NULL, command TEXT NOT NULL, started_at TEXT NOT NULL,
      heartbeat_at TEXT NOT NULL, ended_at TEXT, returncode INTEGER, PRIMARY KEY (run_id, pid)
    );
    CREATE TABLE run_commands (
      id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, command TEXT NOT NULL,
      payload TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, applied_at TEXT
    );
    CREATE TABLE artifacts (
      id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, name TEXT NOT NULL,
      path TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
    );
    CREATE INDEX events_run_id ON events(run_id, id);
    CREATE INDEX run_commands_pending ON run_commands(run_id, status, id);
    """,
    """
    CREATE TABLE agent_sessions (
      id TEXT PRIMARY KEY, provider TEXT NOT NULL, project TEXT NOT NULL, status TEXT NOT NULL,
      workspace TEXT NOT NULL, prompt TEXT NOT NULL, summary TEXT NOT NULL,
      external_id TEXT, process_id INTEGER, started_at TEXT, ended_at TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE agent_session_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, ts TEXT NOT NULL,
      stream TEXT NOT NULL, message TEXT NOT NULL, payload TEXT NOT NULL
    );
    CREATE INDEX agent_sessions_project_status ON agent_sessions(project, status, updated_at);
    CREATE INDEX agent_session_events_session_id ON agent_session_events(session_id, id);
    """,
    """
    CREATE TABLE task_capsules (
      task_id TEXT PRIMARY KEY, capsule TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE INDEX task_capsules_task_id ON task_capsules(task_id);
    """,
    """
    CREATE TABLE queue_items (
      id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, priority INTEGER NOT NULL,
      payload TEXT NOT NULL, result_id TEXT, error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
      lease_owner TEXT, leased_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE INDEX queue_items_status_priority ON queue_items(status, priority, created_at);
    CREATE INDEX queue_items_lease ON queue_items(status, leased_at);
    """,
    """
    CREATE TABLE daemon_processes (
      id TEXT PRIMARY KEY, pid INTEGER NOT NULL, status TEXT NOT NULL, command TEXT NOT NULL,
      poll_seconds REAL NOT NULL, lease_timeout_seconds REAL NOT NULL,
      started_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, stopped_at TEXT, error TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX daemon_processes_status ON daemon_processes(status, heartbeat_at);
    """,
    """
    ALTER TABLE daemon_processes ADD COLUMN last_queue_item_id TEXT;
    """,
    """
    CREATE TABLE duels (
      id TEXT PRIMARY KEY, project TEXT NOT NULL, prompt TEXT NOT NULL, providers TEXT NOT NULL,
      status TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', report_path TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE duel_participants (
      duel_id TEXT NOT NULL, provider TEXT NOT NULL, session_id TEXT, status TEXT NOT NULL,
      workspace TEXT NOT NULL DEFAULT '', changed_files TEXT NOT NULL DEFAULT '[]',
      diff_stat TEXT NOT NULL DEFAULT '', validation_ok INTEGER, validation_summary TEXT NOT NULL DEFAULT '',
      runtime_seconds REAL NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT '',
      open_risks TEXT NOT NULL DEFAULT '[]', artifact_path TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      PRIMARY KEY (duel_id, provider)
    );
    CREATE INDEX duels_project_status ON duels(project, status, updated_at);
    CREATE INDEX duel_participants_session_id ON duel_participants(session_id);
    """,
    """
    CREATE TABLE adoption_decisions (
      id TEXT PRIMARY KEY, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
      provider TEXT, session_id TEXT, project TEXT NOT NULL,
      source_workspace TEXT NOT NULL, destination_workspace TEXT NOT NULL,
      status TEXT NOT NULL, changed_files TEXT NOT NULL DEFAULT '[]',
      summary TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL
    );
    CREATE INDEX adoption_decisions_target ON adoption_decisions(target_type, target_id, created_at);
    CREATE INDEX adoption_decisions_project ON adoption_decisions(project, created_at);
    """,
    """
    CREATE TABLE cross_reviews (
      id TEXT PRIMARY KEY, duel_id TEXT NOT NULL, status TEXT NOT NULL,
      summary TEXT NOT NULL DEFAULT '', report_path TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE cross_review_critiques (
      cross_review_id TEXT NOT NULL, duel_id TEXT NOT NULL,
      reviewer_provider TEXT NOT NULL, subject_provider TEXT NOT NULL,
      session_id TEXT, status TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
      findings TEXT NOT NULL DEFAULT '[]', artifact_path TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      PRIMARY KEY (cross_review_id, reviewer_provider, subject_provider)
    );
    CREATE INDEX cross_reviews_duel_status ON cross_reviews(duel_id, status, updated_at);
    CREATE INDEX cross_review_critiques_session_id ON cross_review_critiques(session_id);
    """,
]

ACTIVE_STATUSES = (
    "queued", "planning", "preflight", "checkpointing", "running", "paused", "stopping",
    "validating", "reviewing", "retryable_failure", "awaiting_decision",
)
ACTIVE_AGENT_SESSION_STATUSES = ("queued", "running", "stopping")
ACTIVE_DUEL_STATUSES = ("queued", "running", "awaiting_decision")
ACTIVE_CROSS_REVIEW_STATUSES = ("queued", "running", "awaiting_decision")


class RunLockedError(RuntimeError):
    pass


class Store:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = state_dir / "orch.db"
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                legacy = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
                ).fetchone()
                if legacy:
                    version = 1
                    conn.execute("PRAGMA user_version = 1")
            for index, migration in enumerate(MIGRATIONS[version:], start=version + 1):
                conn.executescript(migration)
                conn.execute(f"PRAGMA user_version = {index}")

    @contextmanager
    def run_lock(self, run_id: str, blocking: bool = False) -> Iterator[None]:
        lock_dir = self.state_dir / "locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        with (lock_dir / f"{run_id}.lock").open("a+") as handle:
            flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(handle, flags)
            except BlockingIOError as exc:
                raise RunLockedError(f"Run is already being operated: {run_id}") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def create_task(
        self,
        project: str,
        title: str,
        prompt: str,
        success_criteria: str,
        profile: str | None,
        capsule: TaskCapsule | None = None,
    ) -> TaskRecord:
        task = TaskRecord(id=str(uuid.uuid4()), project=project, title=title, prompt=prompt,
                          success_criteria=success_criteria, profile=profile, created_at=datetime.now(UTC))
        with self._connect() as conn:
            conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (task.id, task.project, task.title, task.prompt, task.success_criteria,
                          task.profile, task.created_at.isoformat()))
            self._upsert_task_capsule(conn, task.id, capsule or self._default_capsule(task))
        return task

    def get_task(self, task_id: str) -> TaskRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Task not found: {task_id}")
        return TaskRecord.model_validate(dict(row))

    def list_tasks(self) -> list[TaskRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
        return [TaskRecord.model_validate(dict(row)) for row in rows]

    def get_task_capsule(self, task_id: str) -> TaskCapsule:
        with self._connect() as conn:
            row = conn.execute("SELECT capsule FROM task_capsules WHERE task_id = ?", (task_id,)).fetchone()
        if row is not None:
            return TaskCapsule.model_validate(json.loads(row["capsule"]))
        task = self.get_task(task_id)
        capsule = self._default_capsule(task)
        self.set_task_capsule(task_id, capsule)
        return capsule

    def set_task_capsule(self, task_id: str, capsule: TaskCapsule) -> None:
        self.get_task(task_id)
        with self._connect() as conn:
            self._upsert_task_capsule(conn, task_id, capsule)

    def _upsert_task_capsule(self, conn: sqlite3.Connection, task_id: str, capsule: TaskCapsule) -> None:
        now = datetime.now(UTC).isoformat()
        conn.execute(
            """INSERT INTO task_capsules (task_id,capsule,created_at,updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET capsule=excluded.capsule, updated_at=excluded.updated_at""",
            (task_id, capsule.model_dump_json(), now, now),
        )

    def _default_capsule(self, task: TaskRecord) -> TaskCapsule:
        return TaskCapsule(
            objective=task.prompt,
            acceptance_criteria=[task.success_criteria] if task.success_criteria else [],
        )

    def create_run(self, task_id: str, project: str, workspace: str, iteration: int = 1) -> RunRecord:
        now = datetime.now(UTC)
        run = RunRecord(id=str(uuid.uuid4()), task_id=task_id, project=project, status="queued",
                        iteration=iteration, workspace=workspace, created_at=now, updated_at=now)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO runs
                (id, task_id, project, status, iteration, workspace, summary, created_at, updated_at,
                 session_id, process_id, heartbeat_at, failure_fingerprint, repeated_failures)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, 0)""",
                (run.id, run.task_id, run.project, run.status, run.iteration, run.workspace, run.summary,
                 run.created_at.isoformat(), run.updated_at.isoformat()))
        return run

    def update_run(self, run_id: str, status: RunStatus, summary: str = "", iteration: int | None = None,
                   failure_fingerprint: str | None = None, repeated_failures: int | None = None) -> None:
        with self._connect() as conn:
            current = conn.execute("SELECT iteration, repeated_failures FROM runs WHERE id = ?", (run_id,)).fetchone()
            if current is None:
                raise KeyError(f"Run not found: {run_id}")
            conn.execute(
                """UPDATE runs SET status=?, summary=?, iteration=?, updated_at=?,
                   failure_fingerprint=?, repeated_failures=? WHERE id=?""",
                (status, summary, iteration if iteration is not None else current["iteration"],
                 datetime.now(UTC).isoformat(), failure_fingerprint,
                 repeated_failures if repeated_failures is not None else current["repeated_failures"], run_id))

    def get_run(self, run_id: str) -> RunRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Run not found: {run_id}")
        return RunRecord.model_validate(dict(row))

    def list_runs(self, active_only: bool = False) -> list[RunRecord]:
        query, params = "SELECT * FROM runs", ()
        if active_only:
            query += f" WHERE status IN ({','.join('?' for _ in ACTIVE_STATUSES)})"
            params = ACTIVE_STATUSES
        with self._connect() as conn:
            rows = conn.execute(query + " ORDER BY updated_at DESC", params).fetchall()
        return [RunRecord.model_validate(dict(row)) for row in rows]

    def active_runs_for_project(self, project: str) -> list[RunRecord]:
        return [run for run in self.list_runs(active_only=True) if run.project == project]

    def find_conflicting_active_run(self, project: str, task_id: str, conflict_domains: list[str]) -> RunRecord | None:
        requested = {domain for domain in conflict_domains if domain}
        if not requested:
            return None
        for run in self.active_runs_for_project(project):
            if run.task_id == task_id:
                continue
            active_capsule = self.get_task_capsule(run.task_id)
            active_domains = self.effective_conflict_domains(active_capsule)
            if requested & active_domains:
                return run
        return None

    def effective_conflict_domains(self, capsule: TaskCapsule) -> set[str]:
        domains = capsule.conflict_domains or capsule.allowed_paths or capsule.expected_files or capsule.protected_paths
        return {domain for domain in domains if domain}

    def enqueue_task(self, task_id: str, priority: int = 100) -> QueueItemRecord:
        self.get_task(task_id)
        return self.enqueue_item("task", {"task_id": task_id}, priority=priority)

    def enqueue_session(
        self,
        provider: str,
        project: str,
        prompt: str,
        *,
        force_worktree: bool = False,
        priority: int = 100,
    ) -> QueueItemRecord:
        return self.enqueue_item(
            "session",
            {
                "provider": provider,
                "project": project,
                "prompt": prompt,
                "force_worktree": force_worktree,
            },
            priority=priority,
        )

    def enqueue_item(self, kind: QueueItemKind, payload: dict, priority: int = 100) -> QueueItemRecord:
        now = datetime.now(UTC)
        item = QueueItemRecord(
            id=str(uuid.uuid4()),
            kind=kind,
            status="queued",
            priority=priority,
            payload=payload,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO queue_items
                (id,kind,status,priority,payload,result_id,error,attempts,lease_owner,leased_at,created_at,updated_at)
                VALUES (?, ?, ?, ?, ?, NULL, '', 0, NULL, NULL, ?, ?)""",
                (
                    item.id,
                    item.kind,
                    item.status,
                    item.priority,
                    json.dumps(item.payload),
                    item.created_at.isoformat(),
                    item.updated_at.isoformat(),
                ),
            )
        return item

    def get_queue_item(self, item_id: str) -> QueueItemRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise KeyError(f"Queue item not found: {item_id}")
        return self._queue_item_from_row(row)

    def list_queue_items(self, status: QueueItemStatus | None = None, limit: int = 100) -> list[QueueItemRecord]:
        query = "SELECT * FROM queue_items"
        params: tuple = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY status, priority ASC, created_at ASC LIMIT ?"
        params = (*params, limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._queue_item_from_row(row) for row in rows]

    def claim_next_queue_item(self, worker_id: str) -> QueueItemRecord | None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT * FROM queue_items WHERE status='queued'
                   ORDER BY priority ASC, created_at ASC LIMIT 1"""
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                """UPDATE queue_items SET status='running', attempts=attempts + 1,
                   lease_owner=?, leased_at=?, updated_at=? WHERE id=?""",
                (worker_id, now, now, row["id"]),
            )
            updated = conn.execute("SELECT * FROM queue_items WHERE id=?", (row["id"],)).fetchone()
            conn.commit()
        return self._queue_item_from_row(updated)

    def reclaim_stale_queue_items(self, older_than_seconds: float) -> int:
        threshold = (datetime.now(UTC) - timedelta(seconds=older_than_seconds)).isoformat()
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE queue_items SET status='queued', lease_owner=NULL, leased_at=NULL,
                   error='', updated_at=? WHERE status='running' AND leased_at IS NOT NULL AND leased_at < ?""",
                (now, threshold),
            )
            return cursor.rowcount

    def heartbeat_queue_item(self, item_id: str, worker_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """UPDATE queue_items SET lease_owner=?, leased_at=?, updated_at=?
                   WHERE id=? AND status='running'""",
                (worker_id, now, now, item_id),
            )

    def complete_queue_item(self, item_id: str, result_id: str | None = None) -> None:
        self._finish_queue_item(item_id, "succeeded", result_id=result_id)

    def fail_queue_item(self, item_id: str, error: str) -> None:
        self._finish_queue_item(item_id, "failed", error=error)

    def cancel_queue_item(self, item_id: str, error: str = "Cancelled.") -> None:
        self._finish_queue_item(item_id, "cancelled", error=error)

    def requeue_queue_item(self, item_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """UPDATE queue_items SET status='queued', lease_owner=NULL, leased_at=NULL,
                   updated_at=? WHERE id=? AND status IN ('running', 'failed')""",
                (now, item_id),
            )

    def _finish_queue_item(
        self,
        item_id: str,
        status: QueueItemStatus,
        *,
        result_id: str | None = None,
        error: str = "",
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """UPDATE queue_items SET status=?, result_id=COALESCE(?, result_id), error=?,
                   lease_owner=NULL, leased_at=NULL, updated_at=? WHERE id=?""",
                (status, result_id, error, now, item_id),
            )

    def _queue_item_from_row(self, row: sqlite3.Row) -> QueueItemRecord:
        return QueueItemRecord.model_validate(
            {
                **dict(row),
                "payload": json.loads(row["payload"]),
                "leased_at": datetime.fromisoformat(row["leased_at"]) if row["leased_at"] else None,
            }
        )

    def create_daemon(
        self,
        pid: int,
        command: list[str],
        poll_seconds: float,
        lease_timeout_seconds: float,
        *,
        daemon_id: str | None = None,
    ) -> DaemonRecord:
        now = datetime.now(UTC)
        record = DaemonRecord(
            id=daemon_id or str(uuid.uuid4()),
            pid=pid,
            status="running",
            command=command,
            poll_seconds=poll_seconds,
            lease_timeout_seconds=lease_timeout_seconds,
            started_at=now,
            heartbeat_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO daemon_processes
                (id,pid,status,command,poll_seconds,lease_timeout_seconds,started_at,heartbeat_at,stopped_at,error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, '')""",
                (
                    record.id,
                    record.pid,
                    record.status,
                    json.dumps(record.command),
                    record.poll_seconds,
                    record.lease_timeout_seconds,
                    record.started_at.isoformat(),
                    record.heartbeat_at.isoformat(),
                ),
            )
        return record

    def get_daemon(self, daemon_id: str) -> DaemonRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM daemon_processes WHERE id = ?", (daemon_id,)).fetchone()
        if row is None:
            raise KeyError(f"Daemon not found: {daemon_id}")
        return self._daemon_from_row(row)

    def list_daemons(self, active_only: bool = False, limit: int = 20) -> list[DaemonRecord]:
        query = "SELECT * FROM daemon_processes"
        params: tuple = ()
        if active_only:
            query += " WHERE status IN ('running', 'stopping')"
        query += " ORDER BY started_at DESC LIMIT ?"
        params = (limit,)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._daemon_from_row(row) for row in rows]

    def heartbeat_daemon(self, daemon_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE daemon_processes SET heartbeat_at=? WHERE id=? AND status='running'",
                (now, daemon_id),
            )

    def update_daemon_last_item(self, daemon_id: str, queue_item_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """UPDATE daemon_processes SET last_queue_item_id=?, heartbeat_at=?
                   WHERE id=? AND status='running'""",
                (queue_item_id, now, daemon_id),
            )

    def request_daemon_stop(self, daemon_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE daemon_processes SET status='stopping', heartbeat_at=? WHERE id=? AND status='running'",
                (datetime.now(UTC).isoformat(), daemon_id),
            )

    def finish_daemon(self, daemon_id: str, status: DaemonStatus, error: str = "") -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """UPDATE daemon_processes SET status=?, stopped_at=?, heartbeat_at=?, error=?
                   WHERE id=?""",
                (status, now, now, error, daemon_id),
            )

    def should_stop_daemon(self, daemon_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM daemon_processes WHERE id = ?", (daemon_id,)).fetchone()
        return row is not None and row["status"] == "stopping"

    def _daemon_from_row(self, row: sqlite3.Row) -> DaemonRecord:
        return DaemonRecord.model_validate(
            {
                **dict(row),
                "command": json.loads(row["command"]),
                "started_at": datetime.fromisoformat(row["started_at"]),
                "heartbeat_at": datetime.fromisoformat(row["heartbeat_at"]),
                "stopped_at": datetime.fromisoformat(row["stopped_at"]) if row["stopped_at"] else None,
            }
        )

    def create_duel(self, project: str, prompt: str, providers: list[AgentProvider]) -> DuelRecord:
        now = datetime.now(UTC)
        duel = DuelRecord(
            id=str(uuid.uuid4()),
            project=project,
            prompt=prompt,
            providers=providers,
            status="queued",
            created_at=now,
            updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO duels
                (id,project,prompt,providers,status,summary,report_path,created_at,updated_at)
                VALUES (?, ?, ?, ?, ?, '', '', ?, ?)""",
                (
                    duel.id,
                    duel.project,
                    duel.prompt,
                    json.dumps(duel.providers),
                    duel.status,
                    duel.created_at.isoformat(),
                    duel.updated_at.isoformat(),
                ),
            )
        return duel

    def update_duel(
        self,
        duel_id: str,
        status: DuelStatus,
        summary: str = "",
        *,
        report_path: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE duels SET status=?, summary=?, report_path=COALESCE(?, report_path),
                   updated_at=? WHERE id=?""",
                (status, summary, report_path, datetime.now(UTC).isoformat(), duel_id),
            )

    def get_duel(self, duel_id: str) -> DuelRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM duels WHERE id = ?", (duel_id,)).fetchone()
        if row is None:
            raise KeyError(f"Duel not found: {duel_id}")
        return self._duel_from_row(row)

    def list_duels(self, active_only: bool = False, limit: int = 100) -> list[DuelRecord]:
        query = "SELECT * FROM duels"
        params: tuple = ()
        if active_only:
            query += f" WHERE status IN ({','.join('?' for _ in ACTIVE_DUEL_STATUSES)})"
            params = ACTIVE_DUEL_STATUSES
        query += " ORDER BY updated_at DESC LIMIT ?"
        params = (*params, limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._duel_from_row(row) for row in rows]

    def upsert_duel_participant(
        self,
        duel_id: str,
        provider: AgentProvider,
        status: DuelParticipantStatus,
        *,
        session_id: str | None = None,
        workspace: str = "",
        changed_files: list[str] | None = None,
        diff_stat: str = "",
        validation_ok: bool | None = None,
        validation_summary: str = "",
        runtime_seconds: float = 0.0,
        summary: str = "",
        open_risks: list[str] | None = None,
        artifact_path: str = "",
    ) -> DuelParticipantRecord:
        now = datetime.now(UTC)
        existing = self.get_duel_participant(duel_id, provider, required=False)
        created_at = existing.created_at if existing else now
        record = DuelParticipantRecord(
            duel_id=duel_id,
            provider=provider,
            session_id=session_id if session_id is not None else (existing.session_id if existing else None),
            status=status,
            workspace=workspace or (existing.workspace if existing else ""),
            changed_files=changed_files if changed_files is not None else (existing.changed_files if existing else []),
            diff_stat=diff_stat or (existing.diff_stat if existing else ""),
            validation_ok=validation_ok if validation_ok is not None else (existing.validation_ok if existing else None),
            validation_summary=validation_summary or (existing.validation_summary if existing else ""),
            runtime_seconds=runtime_seconds or (existing.runtime_seconds if existing else 0.0),
            summary=summary or (existing.summary if existing else ""),
            open_risks=open_risks if open_risks is not None else (existing.open_risks if existing else []),
            artifact_path=artifact_path or (existing.artifact_path if existing else ""),
            created_at=created_at,
            updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO duel_participants
                (duel_id,provider,session_id,status,workspace,changed_files,diff_stat,validation_ok,
                 validation_summary,runtime_seconds,summary,open_risks,artifact_path,created_at,updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(duel_id, provider) DO UPDATE SET
                  session_id=excluded.session_id, status=excluded.status, workspace=excluded.workspace,
                  changed_files=excluded.changed_files, diff_stat=excluded.diff_stat,
                  validation_ok=excluded.validation_ok, validation_summary=excluded.validation_summary,
                  runtime_seconds=excluded.runtime_seconds, summary=excluded.summary,
                  open_risks=excluded.open_risks, artifact_path=excluded.artifact_path,
                  updated_at=excluded.updated_at""",
                (
                    record.duel_id,
                    record.provider,
                    record.session_id,
                    record.status,
                    record.workspace,
                    json.dumps(record.changed_files),
                    record.diff_stat,
                    None if record.validation_ok is None else int(record.validation_ok),
                    record.validation_summary,
                    record.runtime_seconds,
                    record.summary,
                    json.dumps(record.open_risks),
                    record.artifact_path,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
        return record

    def get_duel_participant(
        self,
        duel_id: str,
        provider: AgentProvider,
        *,
        required: bool = True,
    ) -> DuelParticipantRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM duel_participants WHERE duel_id = ? AND provider = ?",
                (duel_id, provider),
            ).fetchone()
        if row is None:
            if required:
                raise KeyError(f"Duel participant not found: {duel_id}/{provider}")
            return None
        return self._duel_participant_from_row(row)

    def list_duel_participants(self, duel_id: str) -> list[DuelParticipantRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM duel_participants WHERE duel_id = ? ORDER BY created_at ASC",
                (duel_id,),
            ).fetchall()
        return [self._duel_participant_from_row(row) for row in rows]

    def _duel_from_row(self, row: sqlite3.Row) -> DuelRecord:
        return DuelRecord.model_validate({**dict(row), "providers": json.loads(row["providers"])})

    def _duel_participant_from_row(self, row: sqlite3.Row) -> DuelParticipantRecord:
        validation_ok = row["validation_ok"]
        return DuelParticipantRecord.model_validate(
            {
                **dict(row),
                "changed_files": json.loads(row["changed_files"]),
                "open_risks": json.loads(row["open_risks"]),
                "validation_ok": None if validation_ok is None else bool(validation_ok),
            }
        )

    def create_adoption_decision(
        self,
        *,
        target_type: AdoptionTargetType,
        target_id: str,
        project: str,
        source_workspace: str,
        destination_workspace: str,
        status: AdoptionStatus,
        provider: AgentProvider | None = None,
        session_id: str | None = None,
        changed_files: list[str] | None = None,
        summary: str = "",
        error: str = "",
    ) -> AdoptionRecord:
        record = AdoptionRecord(
            id=str(uuid.uuid4()),
            target_type=target_type,
            target_id=target_id,
            provider=provider,
            session_id=session_id,
            project=project,
            source_workspace=source_workspace,
            destination_workspace=destination_workspace,
            status=status,
            changed_files=changed_files or [],
            summary=summary,
            error=error,
            created_at=datetime.now(UTC),
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO adoption_decisions
                (id,target_type,target_id,provider,session_id,project,source_workspace,destination_workspace,
                 status,changed_files,summary,error,created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.id,
                    record.target_type,
                    record.target_id,
                    record.provider,
                    record.session_id,
                    record.project,
                    record.source_workspace,
                    record.destination_workspace,
                    record.status,
                    json.dumps(record.changed_files),
                    record.summary,
                    record.error,
                    record.created_at.isoformat(),
                ),
            )
        return record

    def get_adoption_decision(self, decision_id: str) -> AdoptionRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM adoption_decisions WHERE id = ?", (decision_id,)).fetchone()
        if row is None:
            raise KeyError(f"Adoption decision not found: {decision_id}")
        return self._adoption_from_row(row)

    def list_adoption_decisions(self, limit: int = 100) -> list[AdoptionRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM adoption_decisions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._adoption_from_row(row) for row in rows]

    def _adoption_from_row(self, row: sqlite3.Row) -> AdoptionRecord:
        return AdoptionRecord.model_validate({**dict(row), "changed_files": json.loads(row["changed_files"])})

    def create_cross_review(self, duel_id: str) -> CrossReviewRecord:
        self.get_duel(duel_id)
        now = datetime.now(UTC)
        record = CrossReviewRecord(
            id=str(uuid.uuid4()),
            duel_id=duel_id,
            status="queued",
            created_at=now,
            updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO cross_reviews
                (id,duel_id,status,summary,report_path,created_at,updated_at)
                VALUES (?, ?, ?, '', '', ?, ?)""",
                (record.id, record.duel_id, record.status, record.created_at.isoformat(), record.updated_at.isoformat()),
            )
        return record

    def update_cross_review(
        self,
        cross_review_id: str,
        status: CrossReviewStatus,
        summary: str = "",
        *,
        report_path: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE cross_reviews SET status=?, summary=?, report_path=COALESCE(?, report_path),
                   updated_at=? WHERE id=?""",
                (status, summary, report_path, datetime.now(UTC).isoformat(), cross_review_id),
            )

    def get_cross_review(self, cross_review_id: str) -> CrossReviewRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM cross_reviews WHERE id = ?", (cross_review_id,)).fetchone()
        if row is None:
            raise KeyError(f"Cross-review not found: {cross_review_id}")
        return CrossReviewRecord.model_validate(dict(row))

    def list_cross_reviews(
        self,
        *,
        duel_id: str | None = None,
        active_only: bool = False,
        limit: int = 100,
    ) -> list[CrossReviewRecord]:
        query = "SELECT * FROM cross_reviews"
        params: tuple = ()
        clauses: list[str] = []
        if duel_id:
            clauses.append("duel_id = ?")
            params = (*params, duel_id)
        if active_only:
            clauses.append(f"status IN ({','.join('?' for _ in ACTIVE_CROSS_REVIEW_STATUSES)})")
            params = (*params, *ACTIVE_CROSS_REVIEW_STATUSES)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params = (*params, limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [CrossReviewRecord.model_validate(dict(row)) for row in rows]

    def upsert_cross_review_critique(
        self,
        cross_review_id: str,
        duel_id: str,
        reviewer_provider: AgentProvider,
        subject_provider: AgentProvider,
        status: CrossReviewCritiqueStatus,
        *,
        session_id: str | None = None,
        summary: str = "",
        findings: list[str] | None = None,
        artifact_path: str = "",
    ) -> CrossReviewCritiqueRecord:
        now = datetime.now(UTC)
        existing = self.get_cross_review_critique(
            cross_review_id,
            reviewer_provider,
            subject_provider,
            required=False,
        )
        created_at = existing.created_at if existing else now
        record = CrossReviewCritiqueRecord(
            cross_review_id=cross_review_id,
            duel_id=duel_id,
            reviewer_provider=reviewer_provider,
            subject_provider=subject_provider,
            session_id=session_id if session_id is not None else (existing.session_id if existing else None),
            status=status,
            summary=summary or (existing.summary if existing else ""),
            findings=findings if findings is not None else (existing.findings if existing else []),
            artifact_path=artifact_path or (existing.artifact_path if existing else ""),
            created_at=created_at,
            updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO cross_review_critiques
                (cross_review_id,duel_id,reviewer_provider,subject_provider,session_id,status,summary,
                 findings,artifact_path,created_at,updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cross_review_id, reviewer_provider, subject_provider) DO UPDATE SET
                  session_id=excluded.session_id, status=excluded.status, summary=excluded.summary,
                  findings=excluded.findings, artifact_path=excluded.artifact_path,
                  updated_at=excluded.updated_at""",
                (
                    record.cross_review_id,
                    record.duel_id,
                    record.reviewer_provider,
                    record.subject_provider,
                    record.session_id,
                    record.status,
                    record.summary,
                    json.dumps(record.findings),
                    record.artifact_path,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
        return record

    def get_cross_review_critique(
        self,
        cross_review_id: str,
        reviewer_provider: AgentProvider,
        subject_provider: AgentProvider,
        *,
        required: bool = True,
    ) -> CrossReviewCritiqueRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM cross_review_critiques
                   WHERE cross_review_id=? AND reviewer_provider=? AND subject_provider=?""",
                (cross_review_id, reviewer_provider, subject_provider),
            ).fetchone()
        if row is None:
            if required:
                raise KeyError(
                    f"Cross-review critique not found: {cross_review_id}/{reviewer_provider}/{subject_provider}"
                )
            return None
        return self._cross_review_critique_from_row(row)

    def list_cross_review_critiques(self, cross_review_id: str) -> list[CrossReviewCritiqueRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cross_review_critiques WHERE cross_review_id=? ORDER BY created_at ASC",
                (cross_review_id,),
            ).fetchall()
        return [self._cross_review_critique_from_row(row) for row in rows]

    def _cross_review_critique_from_row(self, row: sqlite3.Row) -> CrossReviewCritiqueRecord:
        return CrossReviewCritiqueRecord.model_validate({**dict(row), "findings": json.loads(row["findings"])})

    def create_agent_session(
        self,
        provider: AgentProvider,
        project: str,
        workspace: str,
        prompt: str,
        *,
        session_id: str | None = None,
    ) -> AgentSessionRecord:
        now = datetime.now(UTC)
        session = AgentSessionRecord(
            id=session_id or str(uuid.uuid4()),
            provider=provider,
            project=project,
            status="queued",
            workspace=workspace,
            prompt=prompt,
            created_at=now,
            updated_at=now,
        )
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO agent_sessions
                (id, provider, project, status, workspace, prompt, summary, external_id, process_id,
                 started_at, ended_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)""",
                (
                    session.id,
                    session.provider,
                    session.project,
                    session.status,
                    session.workspace,
                    session.prompt,
                    session.summary,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                ),
            )
        return session

    def get_agent_session(self, session_id: str) -> AgentSessionRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(f"Agent session not found: {session_id}")
        return AgentSessionRecord.model_validate(dict(row))

    def list_agent_sessions(self, active_only: bool = False) -> list[AgentSessionRecord]:
        query, params = "SELECT * FROM agent_sessions", ()
        if active_only:
            query += f" WHERE status IN ({','.join('?' for _ in ACTIVE_AGENT_SESSION_STATUSES)})"
            params = ACTIVE_AGENT_SESSION_STATUSES
        with self._connect() as conn:
            rows = conn.execute(query + " ORDER BY updated_at DESC", params).fetchall()
        return [AgentSessionRecord.model_validate(dict(row)) for row in rows]

    def active_agent_sessions_for_project(self, project: str) -> list[AgentSessionRecord]:
        return [session for session in self.list_agent_sessions(active_only=True) if session.project == project]

    def start_agent_session(self, session_id: str, pid: int) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """UPDATE agent_sessions SET status='running', process_id=?, started_at=?,
                   updated_at=? WHERE id=?""",
                (pid, now, now, session_id),
            )

    def update_agent_session_external_id(self, session_id: str, external_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE agent_sessions SET external_id=?, updated_at=? WHERE id=?",
                (external_id, datetime.now(UTC).isoformat(), session_id),
            )

    def finish_agent_session(
        self,
        session_id: str,
        status: AgentSessionStatus,
        summary: str,
        *,
        external_id: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """UPDATE agent_sessions SET status=?, summary=?, external_id=COALESCE(?, external_id),
                   process_id=NULL, ended_at=?, updated_at=? WHERE id=?""",
                (status, summary, external_id, now, now, session_id),
            )

    def update_agent_session_status(self, session_id: str, status: AgentSessionStatus, summary: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE agent_sessions SET status=?, summary=?, updated_at=? WHERE id=?",
                (status, summary, datetime.now(UTC).isoformat(), session_id),
            )

    def append_agent_session_event(self, event: AgentSessionEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_session_events (session_id,ts,stream,message,payload) VALUES (?,?,?,?,?)",
                (
                    event.session_id,
                    event.ts.isoformat(),
                    event.stream,
                    event.message,
                    json.dumps(event.payload),
                ),
            )

    def list_agent_session_events(self, session_id: str, limit: int = 100) -> list[AgentSessionEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT session_id,ts,stream,message,payload FROM agent_session_events
                   WHERE session_id=? ORDER BY id DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
        return [
            AgentSessionEvent(
                session_id=r["session_id"],
                ts=datetime.fromisoformat(r["ts"]),
                stream=r["stream"],
                message=r["message"],
                payload=json.loads(r["payload"]),
            )
            for r in reversed(rows)
        ]

    def append_event(self, event: EventRecord) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO events (run_id,ts,phase,message,payload) VALUES (?,?,?,?,?)",
                         (event.run_id, event.ts.isoformat(), event.phase, event.message, json.dumps(event.payload)))

    def list_events(self, run_id: str, limit: int = 100) -> list[EventRecord]:
        with self._connect() as conn:
            rows = conn.execute("""SELECT run_id,ts,phase,message,payload FROM events
                                 WHERE run_id=? ORDER BY id DESC LIMIT ?""", (run_id, limit)).fetchall()
        return [EventRecord(run_id=r["run_id"], ts=datetime.fromisoformat(r["ts"]), phase=r["phase"],
                            message=r["message"], payload=json.loads(r["payload"])) for r in reversed(rows)]

    def start_process(self, process: RunProcess) -> None:
        with self._connect() as conn:
            conn.execute("""INSERT INTO run_processes VALUES (?, ?, ?, ?, ?, NULL, NULL)""",
                         (process.run_id, process.pid, json.dumps(process.command),
                          process.started_at.isoformat(), process.heartbeat_at.isoformat()))
            conn.execute("UPDATE runs SET process_id=?, heartbeat_at=?, updated_at=? WHERE id=?",
                         (process.pid, process.heartbeat_at.isoformat(), process.heartbeat_at.isoformat(), process.run_id))

    def heartbeat(self, run_id: str, pid: int) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute("UPDATE run_processes SET heartbeat_at=? WHERE run_id=? AND pid=?", (now, run_id, pid))
            conn.execute("UPDATE runs SET heartbeat_at=?, updated_at=? WHERE id=?", (now, now, run_id))

    def set_session_id(self, run_id: str, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE runs SET session_id=?, updated_at=? WHERE id=?",
                         (session_id, datetime.now(UTC).isoformat(), run_id))

    def finish_process(self, run_id: str, pid: int, returncode: int) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute("UPDATE run_processes SET ended_at=?, returncode=? WHERE run_id=? AND pid=?",
                         (now, returncode, run_id, pid))
            conn.execute("UPDATE runs SET process_id=NULL, heartbeat_at=?, updated_at=? WHERE id=?", (now, now, run_id))

    def enqueue_command(self, run_id: str, command: str, payload: dict | None = None) -> int:
        with self._connect() as conn:
            cursor = conn.execute("""INSERT INTO run_commands
                (run_id,command,payload,status,created_at) VALUES (?,?,?,'pending',?)""",
                (run_id, command, json.dumps(payload or {}), datetime.now(UTC).isoformat()))
            return int(cursor.lastrowid)

    def pending_commands(self, run_id: str) -> list[RunCommand]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM run_commands WHERE run_id=? AND status='pending' ORDER BY id",
                                (run_id,)).fetchall()
        return [RunCommand.model_validate({**dict(r), "payload": json.loads(r["payload"])}) for r in rows]

    def apply_command(self, command_id: int, status: str = "applied") -> None:
        with self._connect() as conn:
            conn.execute("UPDATE run_commands SET status=?, applied_at=? WHERE id=?",
                         (status, datetime.now(UTC).isoformat(), command_id))

    def run_dir(self, run_id: str) -> Path:
        path = self.state_dir / "runs" / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _record_artifact(self, run_id: str, name: str, path: Path) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO artifacts (run_id,name,path,created_at) VALUES (?,?,?,?)",
                         (run_id, name, str(path), datetime.now(UTC).isoformat()))

    def write_artifact(self, run_id: str, name: str, content: str) -> Path:
        path = self.run_dir(run_id) / name
        path.write_text(content)
        self._record_artifact(run_id, name, path)
        return path

    def append_jsonl(self, run_id: str, name: str, rows: Iterable[dict]) -> Path:
        path = self.run_dir(run_id) / name
        new = not path.exists()
        with path.open("a") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        if new:
            self._record_artifact(run_id, name, path)
        return path

    def list_artifacts(self, run_id: str) -> list[Path]:
        with self._connect() as conn:
            rows = conn.execute("SELECT path FROM artifacts WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        return [Path(row["path"]) for row in rows]

from __future__ import annotations

import socket
import time
import uuid
from pathlib import Path

from .agents import AgentSessionRunner
from .config import load_config
from .loop_engine import LoopEngine
from .models import QueueItemRecord
from .store import Store


class QueueWorkerError(RuntimeError):
    pass


class QueueWorker:
    def __init__(self, config_path: Path | None = None, worker_id: str | None = None) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        self.store = Store(self.config.global_config.state_dir)
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4()}"

    def process_once(self) -> QueueItemRecord | None:
        item = self.store.claim_next_queue_item(self.worker_id)
        if item is None:
            return None
        try:
            result_id = self._execute(item)
        except Exception as exc:
            self.store.fail_queue_item(item.id, f"{type(exc).__name__}: {exc}")
            raise
        else:
            self.store.complete_queue_item(item.id, result_id)
            return self.store.get_queue_item(item.id)

    def run_forever(self, poll_seconds: float = 2.0) -> None:
        while True:
            item = self.process_once()
            if item is None:
                time.sleep(poll_seconds)

    def _execute(self, item: QueueItemRecord) -> str:
        if item.kind == "task":
            task_id = item.payload.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise QueueWorkerError("Task queue item missing task_id")
            return LoopEngine(self.config_path).run_task(task_id)
        if item.kind == "session":
            provider = item.payload.get("provider")
            project = item.payload.get("project")
            prompt = item.payload.get("prompt")
            force_worktree = bool(item.payload.get("force_worktree", False))
            if not all(isinstance(value, str) and value for value in (provider, project, prompt)):
                raise QueueWorkerError("Session queue item missing provider, project, or prompt")
            return AgentSessionRunner(self.config_path).start_session(
                provider,
                project,
                prompt,
                force_worktree=force_worktree,
            )
        raise QueueWorkerError(f"Unsupported queue item kind: {item.kind}")

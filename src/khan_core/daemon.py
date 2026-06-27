from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import load_config
from .models import DaemonRecord
from .store import Store


class DaemonSupervisorError(RuntimeError):
    pass


class DaemonSupervisor:
    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        self.store = Store(self.config.global_config.state_dir)
        self._started_processes: dict[str, subprocess.Popen] = {}

    def start(self, poll_seconds: float = 2.0, lease_timeout_seconds: float = 900.0) -> DaemonRecord:
        active = self.active_daemons()
        if active:
            raise DaemonSupervisorError(f"Khan daemon already running: {active[0].id} pid={active[0].pid}")

        daemon_id = self._next_daemon_id()
        command = [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "khan_cli.py"),
            "daemon",
            "run",
            "--daemon-id",
            daemon_id,
            "--poll-seconds",
            str(poll_seconds),
            "--lease-timeout-seconds",
            str(lease_timeout_seconds),
        ]
        if self.config_path:
            command.extend(["--config", str(self.config_path)])

        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        record = self.store.create_daemon(
            process.pid,
            command,
            poll_seconds,
            lease_timeout_seconds,
            daemon_id=daemon_id,
        )
        self._started_processes[record.id] = process
        return record

    def active_daemons(self) -> list[DaemonRecord]:
        active: list[DaemonRecord] = []
        for daemon in self.store.list_daemons(active_only=True):
            if self._is_stale(daemon):
                self._mark_failed_or_restart(daemon, "Heartbeat is stale.", restartable=daemon.status == "running")
                continue
            if self.is_alive(daemon.pid):
                active.append(daemon)
            elif daemon.status in {"running", "stopping"}:
                self._mark_failed_or_restart(daemon, "Process is not alive.", restartable=daemon.status == "running")
        return active

    def stop(self, daemon_id: str | None = None) -> DaemonRecord:
        daemon = self._target_daemon(daemon_id)
        self.store.request_daemon_stop(daemon.id)
        if self.is_alive(daemon.pid):
            try:
                os.killpg(daemon.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self._wait_for_exit(daemon)
        refreshed = self.store.get_daemon(daemon.id)
        if not self.is_alive(refreshed.pid):
            self.store.finish_daemon(refreshed.id, "stopped")
            refreshed = self.store.get_daemon(refreshed.id)
        return refreshed

    def status(self) -> list[DaemonRecord]:
        self.active_daemons()
        return self.store.list_daemons(limit=20)

    def _target_daemon(self, daemon_id: str | None) -> DaemonRecord:
        if daemon_id:
            return self.store.get_daemon(daemon_id)
        active = self.active_daemons()
        if not active:
            raise DaemonSupervisorError("No active Khan daemon.")
        return active[0]

    def _next_daemon_id(self) -> str:
        import uuid

        return str(uuid.uuid4())

    def _wait_for_exit(self, daemon: DaemonRecord, timeout_seconds: float = 2.0) -> None:
        process = self._started_processes.get(daemon.id)
        if process is not None:
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                return
            return

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not self.is_alive(daemon.pid):
                return
            time.sleep(0.05)

    def _mark_failed_or_restart(self, daemon: DaemonRecord, reason: str, *, restartable: bool) -> None:
        self.store.finish_daemon(daemon.id, "failed", reason)
        if restartable and self.config.daemon.restart_on_crash:
            self._restart_daemon(daemon)

    def _restart_daemon(self, daemon: DaemonRecord) -> None:
        daemon_id = self._next_daemon_id()
        command = self._daemon_command_for_new_id(daemon.command, daemon_id)
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        record = self.store.create_daemon(
            process.pid,
            command,
            daemon.poll_seconds,
            daemon.lease_timeout_seconds,
            daemon_id=daemon_id,
        )
        self._started_processes[record.id] = process

    def _daemon_command_for_new_id(self, command: list[str], daemon_id: str) -> list[str]:
        rebuilt = list(command)
        if "--daemon-id" in rebuilt:
            index = rebuilt.index("--daemon-id")
            if index + 1 < len(rebuilt):
                rebuilt[index + 1] = daemon_id
        return rebuilt

    def _is_stale(self, daemon: DaemonRecord) -> bool:
        threshold = timedelta(seconds=self.config.daemon.stale_heartbeat_seconds)
        return datetime.now(UTC) - daemon.heartbeat_at > threshold

    @staticmethod
    def is_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .agent_adapters import DEFAULT_AGENT_REGISTRY, AgentAdapter, AgentAdapterRegistry
from .config import load_config
from .models import AgentProvider, AgentSessionEvent, ProjectConfig
from .store import Store
from .worktree import WorktreeManager


ACTIVE_SESSION_STATUSES = {"queued", "running", "stopping"}


class AgentSessionError(RuntimeError):
    pass


class AgentSessionRunner:
    def __init__(self, config_path: Path | None = None, registry: AgentAdapterRegistry | None = None) -> None:
        self.config = load_config(config_path)
        self.store = Store(self.config.global_config.state_dir)
        self.worktrees = WorktreeManager(self.config.global_config.state_dir)
        self.registry = registry or DEFAULT_AGENT_REGISTRY

    def start_session(
        self,
        provider: AgentProvider,
        project_name: str,
        prompt: str,
        *,
        force_worktree: bool = False,
        workspace: Path | None = None,
        parent_session_id: str | None = None,
        resume_external_session_id: str | None = None,
        resume_message: str | None = None,
    ) -> str:
        try:
            adapter = self.registry.get(provider)
        except KeyError as exc:
            raise AgentSessionError(str(exc)) from exc
        project = self._project(project_name)
        with self.store.run_lock("__agent_scheduler__", blocking=True):
            active_count = len(self.store.list_runs(active_only=True)) + len(self.store.list_agent_sessions(active_only=True))
            if active_count >= self.config.global_config.max_concurrent_runs:
                raise RuntimeError("Maximum concurrent run limit reached.")
            if workspace is None:
                active_project_sessions = self.store.active_agent_sessions_for_project(project.name)
                workspace_key = str(uuid.uuid4())
                workspace, is_worktree = self.worktrees.choose_workspace(
                    project,
                    workspace_key,
                    force_worktree or bool(active_project_sessions),
                )
            else:
                workspace = workspace.expanduser().resolve()
                is_worktree = workspace != project.path.resolve()
                workspace_key = str(uuid.uuid4())
            session = self.store.create_agent_session(
                adapter.name,
                project.name,
                str(workspace),
                prompt,
                session_id=workspace_key,
                parent_session_id=parent_session_id,
            )

        self._event(session.id, "system", "Session queued", {"provider": adapter.name, "workspace": str(workspace)})
        try:
            with self.store.run_lock(f"agent-session-{session.id}"):
                return self._run_session(
                    session.id,
                    adapter,
                    project,
                    workspace,
                    prompt,
                    resume_external_session_id=resume_external_session_id,
                    resume_message=resume_message,
                )
        finally:
            final_status = self.store.get_agent_session(session.id).status
            if is_worktree and final_status not in {"succeeded", "cancelled"}:
                self.worktrees.cleanup(project, workspace, is_worktree=True)

    def cancel_session(self, session_id: str) -> None:
        session = self.store.get_agent_session(session_id)
        if session.status not in ACTIVE_SESSION_STATUSES:
            raise AgentSessionError(f"Agent session {session_id} is not active; status is {session.status}")
        if session.process_id is None:
            self.store.update_agent_session_status(session_id, "cancelled", "Cancelled before process start.")
            self._event(session_id, "system", "Cancelled before process start", {})
            return
        self.store.update_agent_session_status(session_id, "stopping", "Cancel requested.")
        self._event(session_id, "system", "Cancel requested", {"pid": session.process_id})
        try:
            os.killpg(session.process_id, signal.SIGTERM)
        except ProcessLookupError:
            self.store.finish_agent_session(session_id, "cancelled", "Process was already gone.")

    def _run_session(
        self,
        session_id: str,
        adapter: AgentAdapter,
        project: ProjectConfig,
        workspace: Path,
        prompt: str,
        *,
        resume_external_session_id: str | None = None,
        resume_message: str | None = None,
    ) -> str:
        command = None
        if resume_external_session_id and resume_message and getattr(adapter, "supports_steering", lambda: False)():
            command = getattr(adapter, "resume_command", lambda **_: None)(
                config=self.config,
                project=project,
                store=self.store,
                workspace=workspace,
                prompt=prompt,
                session_id=session_id,
                external_session_id=resume_external_session_id,
                message=resume_message,
            )
            if command is None:
                command = getattr(adapter, "send_message", lambda **_: None)(
                    config=self.config,
                    project=project,
                    store=self.store,
                    workspace=workspace,
                    prompt=prompt,
                    session_id=session_id,
                    external_session_id=resume_external_session_id,
                    message=resume_message,
                )
        if command is None:
            command = adapter.build_command(
                config=self.config,
                project=project,
                store=self.store,
                workspace=workspace,
                prompt=prompt,
                session_id=session_id,
            )
        self._event(session_id, "system", "Starting agent process", {"command": command.argv})
        process = subprocess.Popen(
            command.argv,
            stdin=subprocess.PIPE if command.stdin is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
            cwd=workspace,
            env={**os.environ, **project.env},
        )
        self.store.start_agent_session(session_id, process.pid)
        if command.stdin is not None:
            assert process.stdin is not None
            process.stdin.write(command.stdin)
            process.stdin.close()

        selector = selectors.DefaultSelector()
        assert process.stdout is not None and process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")

        stderr_lines: list[str] = []
        last_message = ""
        external_id: str | None = None
        try:
            while process.poll() is None:
                current = self.store.get_agent_session(session_id)
                if current.status == "stopping":
                    self._terminate(process)
                    break
                for key, _ in selector.select(timeout=0.2):
                    line = key.fileobj.readline()
                    if not line:
                        selector.unregister(key.fileobj)
                        continue
                    parsed = adapter.parse_event(line)
                    if key.data == "stderr":
                        stderr_lines.append(parsed.message)
                    if parsed.message:
                        last_message = parsed.message
                    if parsed.external_id:
                        external_id = parsed.external_id
                        self.store.update_agent_session_external_id(session_id, external_id)
                    self._event(session_id, key.data, parsed.message, parsed.payload)
        finally:
            if process.poll() is None:
                self._terminate(process)
            process.wait()
            for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                if stream is None:
                    continue
                for line in stream:
                    parsed = adapter.parse_event(line)
                    if name == "stderr":
                        stderr_lines.append(parsed.message)
                    if parsed.message:
                        last_message = parsed.message
                    if parsed.external_id:
                        external_id = parsed.external_id
                        self.store.update_agent_session_external_id(session_id, external_id)
                    self._event(session_id, name, parsed.message, parsed.payload)
                stream.close()
            selector.close()

        summary = adapter.summarize(command, last_message, stderr_lines)
        current = self.store.get_agent_session(session_id)
        if current.status == "stopping":
            self.store.finish_agent_session(session_id, "cancelled", "Agent session cancelled.", external_id=external_id)
            self._event(session_id, "system", "Session cancelled", {"returncode": process.returncode})
        elif process.returncode == 0:
            self.store.finish_agent_session(session_id, "succeeded", summary, external_id=external_id)
            self._event(session_id, "system", "Session succeeded", {"returncode": process.returncode})
        else:
            failure = "\n".join(stderr_lines).strip() or summary or f"{adapter.name} exited with {process.returncode}"
            self.store.finish_agent_session(session_id, "failed", failure, external_id=external_id)
            self._event(session_id, "system", "Session failed", {"returncode": process.returncode})
        return session_id

    def _event(self, session_id: str, stream: str, message: str, payload: dict[str, Any]) -> None:
        self.store.append_agent_session_event(
            AgentSessionEvent(
                session_id=session_id,
                ts=datetime.now(UTC),
                stream=stream,
                message=message,
                payload=payload,
            )
        )

    def _project(self, project_name: str) -> ProjectConfig:
        if project_name not in self.config.projects:
            raise KeyError(f"Project not found in config: {project_name}")
        return self.config.projects[project_name]

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

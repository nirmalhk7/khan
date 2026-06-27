from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .models import ProjectConfig, ReviewResult, RunProcess, WorkerResult


class CodexInvocationError(RuntimeError):
    pass


class CodexCancelled(CodexInvocationError):
    pass


def _base_command(codex_bin: str) -> list[str]:
    return [codex_bin]


class CodexCLI:
    def __init__(
        self,
        codex_bin: str,
        *,
        model: str = "gpt-5.4-mini",
        reasoning_effort: str = "high",
    ) -> None:
        self.codex_bin = codex_bin
        self.model = model
        self.reasoning_effort = reasoning_effort

    def _exec_options(self) -> list[str]:
        return [
            "-m",
            self.model,
            "-c",
            f'model_reasoning_effort="{self.reasoning_effort}"',
        ]

    def exec_task(
        self,
        workspace: Path,
        prompt: str,
        schema_path: Path,
        output_path: Path,
        project: ProjectConfig,
        extra_env: dict[str, str] | None = None,
        *,
        run_id: str | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_process: Callable[[RunProcess], None] | None = None,
        on_finish: Callable[[int, int], None] | None = None,
        on_heartbeat: Callable[[int], None] | None = None,
        commands: Callable[[], list[tuple[int, str]]] | None = None,
        command_applied: Callable[[int], None] | None = None,
        timeout_seconds: float | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> tuple[WorkerResult, list[dict[str, Any]]]:
        cmd = _base_command(self.codex_bin) + [
            "exec",
            "--json",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            *self._exec_options(),
            "-C",
            str(workspace),
            "-s",
            project.sandbox,
            "-",
        ]
        process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            bufsize=1, start_new_session=True, env={**os.environ, **project.env, **(extra_env or {})},
        )
        started = datetime.now(UTC)
        if on_process and run_id:
            on_process(RunProcess(run_id=run_id, pid=process.pid, command=cmd, started_at=started, heartbeat_at=started))
        assert process.stdin and process.stdout and process.stderr
        process.stdin.write(prompt)
        process.stdin.close()

        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        events: list[dict[str, Any]] = []
        stderr: list[str] = []
        started_mono = last_output = last_heartbeat = time.monotonic()
        paused = False

        try:
            while process.poll() is None:
                now = time.monotonic()
                if timeout_seconds and now - started_mono > timeout_seconds:
                    self._terminate(process)
                    raise CodexInvocationError(f"codex exec exceeded {timeout_seconds:g}s runtime timeout")
                if idle_timeout_seconds and not paused and now - last_output > idle_timeout_seconds:
                    self._terminate(process)
                    raise CodexInvocationError(f"codex exec exceeded {idle_timeout_seconds:g}s idle timeout")
                if on_heartbeat and now - last_heartbeat >= 1:
                    on_heartbeat(process.pid)
                    last_heartbeat = now
                for command_id, command in commands() if commands else []:
                    if command == "pause" and not paused:
                        os.killpg(process.pid, signal.SIGSTOP)
                        paused = True
                    elif command == "resume" and paused:
                        os.killpg(process.pid, signal.SIGCONT)
                        paused = False
                        last_output = time.monotonic()
                    elif command == "cancel":
                        self._terminate(process)
                        if command_applied:
                            command_applied(command_id)
                        raise CodexCancelled("codex exec cancelled")
                    if command_applied:
                        command_applied(command_id)
                for key, _ in selector.select(timeout=0.2):
                    line = key.fileobj.readline()
                    if not line:
                        selector.unregister(key.fileobj)
                        continue
                    last_output = time.monotonic()
                    if key.data == "stderr":
                        stderr.append(line.rstrip())
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        event = {"type": "stdout", "text": line.rstrip()}
                    events.append(event)
                    if on_event:
                        on_event(event)
        finally:
            if process.poll() is None:
                self._terminate(process)
            process.wait()
            selector.close()
            for stream, kind in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                for line in stream:
                    if kind == "stderr":
                        stderr.append(line.rstrip())
                    else:
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            event = {"type": "stdout", "text": line.rstrip()}
                        events.append(event)
                        if on_event:
                            on_event(event)
                stream.close()
            if on_finish:
                on_finish(process.pid, process.returncode)

        if process.returncode != 0:
            raise CodexInvocationError("\n".join(stderr).strip() or "codex exec failed")
        if not output_path.exists():
            raise CodexInvocationError("codex exec did not produce an output-last-message file")
        try:
            result_data = json.loads(output_path.read_text())
        except json.JSONDecodeError as exc:
            raise CodexInvocationError(f"codex exec returned non-JSON final output: {exc}") from exc
        return WorkerResult.model_validate(result_data), events

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

    def review_changes(self, workspace: Path, prompt: str, uncommitted: bool = True,
                       timeout_seconds: float | None = None) -> ReviewResult:
        cmd = _base_command(self.codex_bin) + ["review", *self._exec_options()]
        if uncommitted:
            cmd.append("--uncommitted")
        cmd.append("-")
        try:
            process = subprocess.run(cmd, input=prompt, text=True, capture_output=True, cwd=workspace,
                                     timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise CodexInvocationError("codex review timed out") from exc
        if process.returncode != 0:
            raise CodexInvocationError(process.stderr.strip() or process.stdout.strip() or "codex review failed")
        output = process.stdout.strip()
        verdict: str = "ESCALATE"
        findings: list[str] = []
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        if lines and lines[0].upper().startswith("VERDICT:"):
            value = lines[0].split(":", 1)[1].strip().upper()
            if value in {"PASS", "FIX", "ESCALATE"}:
                verdict, findings = value, lines[1:]
            else:
                findings = lines
        else:
            findings = lines
        return ReviewResult(verdict=verdict, findings=findings, raw_output=output)

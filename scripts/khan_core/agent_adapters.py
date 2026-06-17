from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .models import ConfigFile, ProjectConfig
from .store import Store


@dataclass(frozen=True)
class AgentCommand:
    argv: list[str]
    stdin: str | None = None
    output_path: Path | None = None


@dataclass(frozen=True)
class AgentEvent:
    message: str
    payload: dict[str, Any]
    external_id: str | None = None


class AgentAdapter(Protocol):
    name: str

    def build_command(
        self,
        *,
        config: ConfigFile,
        project: ProjectConfig,
        store: Store,
        workspace: Path,
        prompt: str,
        session_id: str,
    ) -> AgentCommand:
        ...

    def parse_event(self, line: str) -> AgentEvent:
        ...

    def summarize(self, command: AgentCommand, last_message: str, stderr_lines: list[str]) -> str:
        ...


class JsonlAgentAdapter:
    name = ""
    external_id_keys = ("thread_id", "session_id", "chat_id", "chatId", "conversation_id")
    message_keys = ("message", "text", "summary", "content", "type")

    def parse_event(self, line: str) -> AgentEvent:
        text = line.rstrip()
        if not text:
            return AgentEvent(message="", payload={})
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return AgentEvent(message=text, payload={"text": text})
        if not isinstance(payload, dict):
            return AgentEvent(message=text, payload={"value": payload})
        return AgentEvent(
            message=self._message_from_payload(payload),
            payload=payload,
            external_id=self._external_id_from_payload(payload),
        )

    def summarize(self, command: AgentCommand, last_message: str, stderr_lines: list[str]) -> str:
        return last_message or "\n".join(stderr_lines).strip()

    def _message_from_payload(self, payload: dict[str, Any]) -> str:
        for key in self.message_keys:
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return json.dumps(payload, sort_keys=True)

    def _external_id_from_payload(self, payload: dict[str, Any]) -> str | None:
        for key in self.external_id_keys:
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return None


class CodexAgentAdapter(JsonlAgentAdapter):
    name = "codex"

    def build_command(
        self,
        *,
        config: ConfigFile,
        project: ProjectConfig,
        store: Store,
        workspace: Path,
        prompt: str,
        session_id: str,
    ) -> AgentCommand:
        output_path = store.run_dir(session_id) / "codex-last-message.txt"
        return AgentCommand(
            argv=[
                config.global_config.codex_bin,
                "exec",
                "--json",
                "--output-last-message",
                str(output_path),
                "-C",
                str(workspace),
                "-s",
                project.sandbox,
                "-a",
                project.approval_policy,
                "-",
            ],
            stdin=prompt,
            output_path=output_path,
        )

    def summarize(self, command: AgentCommand, last_message: str, stderr_lines: list[str]) -> str:
        if command.output_path and command.output_path.exists():
            content = command.output_path.read_text().strip()
            if content:
                return content
        return super().summarize(command, last_message, stderr_lines)


class CursorAgentAdapter(JsonlAgentAdapter):
    name = "cursor-agent"

    def build_command(
        self,
        *,
        config: ConfigFile,
        project: ProjectConfig,
        store: Store,
        workspace: Path,
        prompt: str,
        session_id: str,
    ) -> AgentCommand:
        return AgentCommand(
            argv=[
                config.global_config.cursor_agent_bin,
                "--print",
                "--output-format",
                "stream-json",
                "--workspace",
                str(workspace),
                "--trust",
                "--sandbox",
                "disabled" if project.sandbox == "danger-full-access" else "enabled",
                prompt,
            ]
        )


class AgentAdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, AgentAdapter] = {}

    def register(self, adapter: AgentAdapter) -> None:
        if not adapter.name:
            raise ValueError("Agent adapter name is required.")
        if adapter.name in self._adapters:
            raise ValueError(f"Agent adapter already registered: {adapter.name}")
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> AgentAdapter:
        try:
            return self._adapters[name]
        except KeyError as exc:
            available = ", ".join(self.names()) or "none"
            raise KeyError(f"Unknown agent provider: {name}. Available providers: {available}") from exc

    def names(self) -> list[str]:
        return sorted(self._adapters)


DEFAULT_AGENT_REGISTRY = AgentAdapterRegistry()
DEFAULT_AGENT_REGISTRY.register(CodexAgentAdapter())
DEFAULT_AGENT_REGISTRY.register(CursorAgentAdapter())


def register_agent_adapter(adapter: AgentAdapter) -> None:
    DEFAULT_AGENT_REGISTRY.register(adapter)


def agent_adapter_names() -> list[str]:
    return DEFAULT_AGENT_REGISTRY.names()

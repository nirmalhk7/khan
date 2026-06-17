# Agent Adapters

Agent sessions are provider-neutral. The session runner owns process lifecycle,
SQLite persistence, stdout/stderr capture, cancellation, worktree selection, and
status updates. Adapters only describe how to run and interpret a provider.

## Built-In Adapters

`CodexAgentAdapter`

- Provider name: `codex`
- Command shape: `codex exec --json --output-last-message ...`
- Prompt delivery: stdin
- Event format: JSONL with fallback for plain text
- External ID keys: `thread_id`, `session_id`, `chat_id`, `chatId`,
  `conversation_id`

`CursorAgentAdapter`

- Provider name: `cursor-agent`
- Command shape: `cursor-agent --print --output-format stream-json`
- Prompt delivery: argv prompt
- Event format: JSONL with fallback for plain text
- Uses `--workspace`, `--trust`, and sandbox mode derived from project config.

## Adapter Protocol

Implement `AgentAdapter` through the public Khan adapter aliases.

```python
from pathlib import Path

from khan.agent_adapters import AgentCommand, AgentEvent
from khan.models import ConfigFile, ProjectConfig
from khan.store import Store


class MyAgentAdapter:
    name = "my-agent"

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
            argv=["my-agent", "--workspace", str(workspace), "--json"],
            stdin=prompt,
        )

    def parse_event(self, line: str) -> AgentEvent:
        return AgentEvent(message=line.rstrip(), payload={"text": line.rstrip()})

    def summarize(self, command: AgentCommand, last_message: str, stderr_lines: list[str]) -> str:
        return last_message or "\n".join(stderr_lines)
```

Register it at process startup:

```python
from khan.agent_adapters import register_agent_adapter

register_agent_adapter(MyAgentAdapter())
```

## JSONL Shortcut

If a provider emits JSONL, subclass `JsonlAgentAdapter` and implement only
`build_command`.

```python
from khan.agent_adapters import AgentCommand, JsonlAgentAdapter


class MyJsonlAgent(JsonlAgentAdapter):
    name = "my-jsonl-agent"

    def build_command(self, **kwargs) -> AgentCommand:
        return AgentCommand(argv=["my-jsonl-agent", "--stream-json"], stdin=kwargs["prompt"])
```

## Adapter Rules

- Keep provider-specific command flags inside the adapter.
- Do not write directly to Khan's database from an adapter unless summary files
  or schemas are truly provider-specific.
- Return stable provider names; they are persisted in SQLite.
- Parse an external session/conversation ID when possible so future resume or
  steering features can target the provider's native session.
- Prefer JSONL output for event capture and operator visibility.

## Current Limitation

The provider-neutral adapter layer is used for `khan session ...`. The older
Codex task loop still calls `CodexCLI` directly because it depends on Codex
structured output and review behavior.

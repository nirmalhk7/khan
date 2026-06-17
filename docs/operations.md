# Operations

This document describes how to run Khan day to day and how to inspect evidence
when something stops.

## Health Checks

```bash
khan doctor
```

Doctor checks:

- configured `codex` binary
- configured `cursor-agent` binary
- `git`
- state directory
- registered project paths
- validation commands configured per project

## Human-Input Stops

A task run enters `needs_human` when Khan detects a condition it should not
resolve alone. Current stop reasons include:

- protected path changed
- worker produced no git diff
- worker reported `blocked` or `needs_human`
- repeated worker or validation failure
- validation retry limit reached
- review round limit reached
- reviewer escalated

When this happens:

```bash
khan run status <run-id>
khan run logs <run-id>
khan run artifacts <run-id>
```

If notifications are enabled, Khan also invokes macOS `say` and records a
`notify` event with `sent: true` or `sent: false`.

## Logs and Artifacts

Events are stored in SQLite and exposed through `run logs` or `session logs`.

Task artifacts can include:

- `prompt-<iteration>.md`
- `checkpoint.json`
- `codex-output.jsonl`
- `last-message.json`
- `validation-<iteration>.json`
- `review-<round>.md`
- `summary.md`

Agent sessions store stdout, stderr, and system events in
`agent_session_events`.

## Queue And Daemon

Queue task runs:

```bash
khan task enqueue <task-id>
```

Queue agent sessions:

```bash
khan session enqueue cursor-agent <project> --prompt "..."
```

Inspect and operate the queue:

```bash
khan queue list
khan queue work --once
khan queue work
khan queue cancel <queue-item-id>
khan queue requeue <queue-item-id>
khan daemon start
khan daemon status
khan daemon stop
khan daemon run
```

`queue work` runs a foreground worker loop. `daemon start` records and launches
a detached daemon process. `daemon status` shows PID, heartbeat, status, last
processed queue item, and errors. Khan reclaims stale running queue leases before
workers claim new work.

Use the same `--config <path>` with `daemon start`, `daemon status`, and
`daemon stop` when operating a non-default Khan state store.

## Cancellation

Task-loop cancellation:

```bash
khan run cancel <run-id>
```

This queues a cancel command. The Codex worker polls pending commands and
terminates its process group.

Agent-session cancellation:

```bash
khan session cancel <session-id>
```

This marks the session as `stopping` and sends `SIGTERM` to the provider process
group. If the process does not exit promptly, Khan escalates to `SIGKILL`.

## Worktrees

Khan can run in place or in a managed git worktree.

- `workspace_mode: in_place`: use the project path directly.
- `workspace_mode: worktree`: always create a worktree.
- `workspace_mode: auto`: create a worktree when needed.

Use `khan session start ... --worktree` to force a worktree for a direct agent
session.

## Testing

```bash
make test
```

The test suite uses fake Codex, Cursor Agent, and `say` binaries. It does not
call real external agents.

Coverage currently includes:

- SQLite migrations and legacy migration
- run locking and process lifecycle
- Codex streaming and cancellation
- protected-path detection from git diff
- task capsule persistence
- conflict-domain scheduling
- allowed-path stop conditions
- attention routing and metrics
- durable queue lifecycle
- queue worker success and failure handling
- detached daemon start/status/stop
- stale queue lease recovery
- agent session persistence
- built-in Codex and Cursor Agent sessions
- custom adapter registration
- input-needed notification through fake `say`

## Known Operational Gaps

- No automatic crash restart policy or daemon log tailing yet.
- No web UI.
- TUI cannot yet perform actions from selected rows.
- Provider-neutral sessions cannot yet resume or steer an existing external
  chat/session.
- No automatic cleanup policy for successful worktrees.

# CLI Reference

Run `khan --help` or any subcommand with `--help` for generated help.

## Setup

```bash
khan init
khan doctor
```

`init` writes the default config and initializes the SQLite store. `doctor`
checks configured binaries, git, the state directory, and registered projects.

## Projects

```bash
khan project add <name> <path>
khan project list
```

`project add` stores a project in config and infers basic validation commands
from files such as `package.json`, `pyproject.toml`, `Cargo.toml`, and
`Makefile`.

## Tasks

```bash
khan task create <project> --title "..." --prompt "..." --success "..."
khan task capsule <task-id>
khan list-tasks
khan task run <task-id>
khan task enqueue <task-id>
khan task retry <run-id>
```

A task is an objective plus success criteria. `task run` creates a run, chooses
a workspace, invokes the Codex loop, records artifacts, validates changes, and
optionally reviews them.

`task create` can also persist a task capsule:

```bash
khan task create khan \
  --title "Scoped docs" \
  --prompt "Update docs." \
  --success "Docs are accurate." \
  --allowed-path README.md \
  --verify "make test" \
  --conflict-domain docs
```

## Runs

```bash
khan watch <run-id>
khan run status <run-id>
khan run logs <run-id> --limit 100
khan run artifacts <run-id>
khan run pause <run-id>
khan run resume <run-id>
khan run cancel <run-id>
khan review <run-id>
```

Run controls enqueue commands in SQLite. The Codex process polls pending
commands while running.

Status values include:

- `queued`
- `preflight`
- `running`
- `paused`
- `stopping`
- `validating`
- `reviewing`
- `retryable_failure`
- `needs_human`
- `succeeded`
- `failed`
- `cancelled`

## Agent Sessions

```bash
khan session providers
khan session start codex <project> --prompt "..."
khan session start cursor-agent <project> --prompt "..."
khan session start cursor-agent <project> --prompt "..." --worktree
khan session enqueue cursor-agent <project> --prompt "..."
khan session list
khan session list --active
khan session status <session-id>
khan session logs <session-id> --limit 100
khan session cancel <session-id>
```

`session start` is synchronous today: it starts the headless provider process,
streams events into SQLite, and returns when the process exits. Use `--worktree`
to force an isolated git worktree.

`session enqueue` records the session request as a queue item for `khan queue
work` or `khan daemon`.

Session status values include:

- `queued`
- `running`
- `stopping`
- `succeeded`
- `failed`
- `cancelled`

## TUI

```bash
khan queue list
khan queue work --once
khan queue work
khan queue cancel <queue-item-id>
khan queue requeue <queue-item-id>
khan daemon
khan tui
khan attention
khan metrics
```

`queue work --once` processes at most one item. `queue work` and `daemon` run a
foreground worker loop.

The TUI currently shows registered projects, recent task runs, recent agent
sessions, and active work. It is a scaffold for the operator console and does
not yet expose all CLI actions.

`attention` shows runs, sessions, and queue items sorted by operator priority.
`metrics` prints JSON counts for run statuses, session statuses, queue statuses,
and attention classes.

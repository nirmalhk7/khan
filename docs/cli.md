# CLI Reference

Run `khan --help` or any subcommand with `--help` for generated help.

Khan keeps a small kubectl-style resource layer for operator muscle memory:

- `khan get` -> active cluster view
- `khan get runs`
- `khan get sessions`
- `khan get queue`
- `khan get duels`
- `khan get pipelines`
- `khan get cross-reviews`
- `khan get adoptions`
- `khan run watch` -> `khan watch`

Generate shell completion with:

```bash
khan completion bash
khan completion zsh
khan completion fish
khan completion powershell
khan completion pwsh
```

The generated script includes completion for the full Khan command tree,
including nested commands and options. Pipe it into your shell profile or the
appropriate completion directory for your shell.

## Setup

```bash
khan init
khan doctor
```

`init` writes the default config and initializes the SQLite store. `doctor`
checks configured binaries, git, the state directory, and registered projects.

## Ask

```bash
khan ask <project-or-path> "Implement this task."
khan ask . "Implement this task." --mode queue
khan ask . "Implement this task." --mode single
khan ask . "Implement this task." --verify "make test" --accept "Tests pass."
```

`ask` is the shortest path from a broad local prompt to a durable Khan task. The
target can be a configured project name or any path inside a local git
repository. If the path is not already configured, Khan discovers the git root,
creates a local project entry, infers validation commands, and persists the
generated task capsule. By default, `ask` runs the full pipeline: Codex creates
a brief, Codex and Cursor Agent build independently, completed candidates are
cross-reviewed, and Khan writes a pipeline report plus `decision-card.md`.

Path-like targets such as `.`, `./repo`, or `/abs/path` are resolved as paths
before project-name lookup. That avoids collisions with accidentally named
projects and makes `khan ask . ...` behave like "use the current repository".

Use `--mode queue` to create the task and queue item without running it in the
current terminal. Use `--mode single` for the original one-agent Codex task
loop. `--enqueue` remains a compatibility alias for queue mode. Use `--title`,
`--success`, `--accept`, `--verify`, and `--profile` when the broad prompt needs
more explicit constraints.

## Inspect

```bash
khan last --kind run
khan inbox
khan show <id>
khan get runs
khan get sessions
khan get
khan summary <id>
khan diff <id>
khan explain <id>
khan explain <id> --json
```

`inbox` shows actionable decision cards, and `show` renders the human-readable
evidence view for runs, sessions, duels, cross-reviews, adoptions, and
pipelines. `summary` renders a
compact markdown record for any stored object, while `diff` shows the recorded
workspace delta for runs, sessions, adoptions, and duel participants. `get`
shows a resource view for active cluster state, or resource-specific tables such
as runs, sessions, and pipelines. `explain` renders the evidence ledger and can
emit JSON for scripts.

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
khan task list
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
khan run watch <run-id>
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

Use `--config <path>` with `session start` and `session enqueue` when operating
a non-default state store.

Session status values include:

- `queued`
- `running`
- `stopping`
- `succeeded`
- `failed`
- `cancelled`

## Provider Duels

```bash
khan duel run <project-or-path> "Implement this task."
khan duel run <project-or-path> "Implement this task." --provider codex --provider cursor-agent
khan duel list
khan duel show <duel-id>
khan duel show <duel-id> --json
khan duel artifacts <duel-id>
```

`duel run` runs each selected provider against the same prompt in an isolated
git worktree, records one parent duel plus one participant per provider,
validates each workspace with the project's configured validation commands, and
writes a `duel-report.md` artifact.

The target can be a configured project name or a local git path. Path targets
are auto-discovered and added to config so broad local work does not require a
separate `project add` step.

Use `--config <path>` on duel commands when operating a non-default state store.

## Cross-Review

```bash
khan cross-review <duel-id>
khan cross-review-list
khan cross-review-list --duel-id <duel-id>
khan cross-review-show <cross-review-id>
khan cross-review-show <cross-review-id> --json
khan cross-review-artifacts <cross-review-id>
```

`cross-review` runs each completed duel provider against the other provider's
diff using the same project review prompt. It records one parent cross-review,
one critique per reviewer/subject pair, Markdown artifacts for each critique,
and a `cross-review-report.md` decision artifact.

## Relay Replay And Steer

```bash
khan relay <project-or-path> "Implement this." --preset "codex-plan cursor-build"
khan relay <project-or-path> "Implement this." --first codex --second cursor-agent
khan replay <run-id> --provider codex
khan replay <run-id> --provider cursor-agent
khan bench prompts.yaml
khan steer <session-id> "Continue this work."
```

`relay` runs two providers in sequence and persists the handoff prompt as an
artifact. Use `--preset` for the named provider pairs that Khan ships with by
default. `steer` continues an existing session when possible or forks a new
continuation session with prior context. `replay` reuses the original task and
project context for a fresh execution, and `bench` runs a YAML list of replay
items and writes JSON/Markdown reports.

## Adoption Decisions

```bash
khan adopt <run-id>
khan adopt <session-id>
khan adopt <pipeline-id> --provider codex
khan adopt <duel-id> --provider codex
khan reject <duel-id> --provider cursor-agent
khan reject <pipeline-id> --provider cursor-agent
khan reject <session-id> --keep-worktree
khan adoption list
```

`adopt` copies changes from the recorded source workspace into the configured
project checkout. It refuses to write into a dirty destination worktree unless
`--force` is supplied. Use `--cleanup` to remove the source worktree after a
successful adoption.

`reject` records a rejection and removes the source worktree by default. Use
`--keep-worktree` to preserve it for manual inspection. Every adopt/reject
operation is recorded in SQLite and visible through `adoption list` and
`metrics`.

`adopt` also supports `--validate` to run project validation after copying and
`--commit` / `--commit-message` to create a git commit in the destination repo.

## Queue And Daemon

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

Use `--config <path>` with daemon commands when the daemon should use a
non-default config and state store:

```bash
khan daemon start --config ./khan.yaml
khan daemon status --config ./khan.yaml
khan daemon stop --config ./khan.yaml
```

## Attention And TUI

```bash
khan tui
khan inbox
khan metrics
```

`queue work --once` processes at most one item. `queue work` runs a foreground
worker loop. `daemon start` launches a detached background daemon, while
`daemon run` is the foreground child loop used by the supervisor.

`daemon logs` prints recent daemon lifecycle records in a compact text format.

The TUI currently shows registered projects, recent task runs, recent agent
sessions, and active work. It is a cockpit for the operator console, with
selection-driven evidence, diff, validation, adopt, reject, replay, steer, and
cancel actions.

`attention` shows runs, sessions, queue items, daemons, duels, and cross-reviews sorted by
operator priority. `metrics` prints JSON counts for run statuses, session
statuses, queue statuses, daemon statuses, duel statuses, cross-review statuses,
adoption decisions, and attention classes.

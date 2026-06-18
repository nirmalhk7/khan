# Khan

> "Superior ability breeds superior ambition."  
> "He tasks me."  
> "I shall leave you as you left me."

Khan is a local-first control plane for coding agents. It records projects,
tasks, runs, direct agent sessions, logs, artifacts, process IDs, workspaces,
human-input stops, and adapter events in SQLite so Codex and Cursor Agent can be
orchestrated and inspected from one place.

The name is a nod to Khan Noonien Singh from Star Trek: an intentionally
overpowered operator for work that should be coordinated better than one human
terminal session at a time.

## Status

Khan is early FOSS infrastructure. It is usable as a local CLI today, but richer
TUI, same-session steering, replay/benchmark workflows, crash restart policy,
and deeper multi-agent topologies are still being built.

Currently implemented:

- Durable Codex task loop with validation, optional review, retries, run logs,
  artifacts, process heartbeats, pause/resume/cancel, and protected-path stops.
- Persisted task capsules with acceptance criteria, allowed paths, protected
  paths, verification recipes, blast radius, dependencies, and conflict domains.
- Zero-friction `khan ask <project-or-path> "prompt"` task intake with local git
  project auto-discovery, validation inference, immediate run, and enqueue mode.
- Conflict-domain scheduling guardrails for active runs.
- Durable queue items for task runs and agent sessions, foreground workers,
  detached daemon start/status/stop, stale lease recovery, and last-item
  tracking.
- Provider-neutral session registry for headless agents.
- Built-in agent adapters for `codex` and `cursor-agent`.
- Adapter registry for third-party agents.
- Provider duel records that run Codex and Cursor Agent against the same prompt
  in isolated worktrees, then write side-by-side evidence reports.
- Cross-review records where each duel provider reviews the other provider's
  diff through the same adapter/session layer.
- Manual adopt/reject decisions for run, session, and duel workspaces with dirty
  destination refusal, optional cleanup, and durable decision records.
- Attention router and metrics commands for operator visibility.
- Textual TUI scaffold showing projects, task runs, and agent sessions.
- macOS `say` notification when a task run enters `needs_human`.
- Unit tests using fake Codex, Cursor Agent, and `say` binaries.

## Install

```bash
make setup
```

This creates `.venv`, installs dependencies, installs `khan` into
`~/.local/bin`, initializes state, and runs doctor checks.

Override paths if needed:

```bash
make setup PYTHON=python3.12 INSTALL_DIR="$HOME/.local/bin"
```

Run tests:

```bash
make test
```

## Quickstart

```bash
khan init
khan doctor
khan project add khan /Users/nirmalhk7/Documents/DevWorld/khan
khan project list
```

Run a broad local task without pre-registering the repo:

```bash
khan ask . "Update the docs for the current orchestration features."
khan ask . "Queue this for later validation." --enqueue
```

Create a durable Codex task with a capsule:

```bash
khan task create khan \
  --title "Improve docs" \
  --prompt "Update documentation for the current agent orchestration features." \
  --success "README and docs describe setup, sessions, adapters, and operations." \
  --accept "Docs are accurate for the current CLI." \
  --allowed-path README.md \
  --allowed-path docs \
  --verify "make test" \
  --conflict-domain docs
```

Run and inspect it:

```bash
khan list-tasks
khan task capsule <task-id>
khan task run <task-id>
khan task enqueue <task-id>
khan queue work --once
khan daemon start
khan daemon status
khan watch <run-id>
khan attention
khan metrics
```

Start one-shot provider-neutral agent sessions:

```bash
khan session providers
khan session start codex khan --prompt "Inspect this repo and summarize the architecture."
khan session start cursor-agent khan --prompt "Inspect this repo and summarize the architecture."
khan session enqueue cursor-agent khan --prompt "Inspect this repo and summarize the architecture."
khan queue list
khan session list
khan session logs <session-id>
```

Run a provider duel and inspect the evidence:

```bash
khan duel run khan "Implement the feature in the safest way you can."
khan duel show <duel-id>
khan duel artifacts <duel-id>
khan cross-review <duel-id>
khan cross-review-show <cross-review-id>
khan adopt <duel-id> --provider codex
khan reject <duel-id> --provider cursor-agent
khan adoption list
```

Control task runs:

```bash
khan run status <run-id>
khan run logs <run-id>
khan run artifacts <run-id>
khan run pause <run-id>
khan run resume <run-id>
khan run cancel <run-id>
```

Open the operator console scaffold:

```bash
khan tui
```

## Configuration

Default config is written to `~/.khan/config.yaml`, or to repo-local
`.khan/config.yaml` when the home directory is not writable.

Set `KHAN_HOME` to force a state directory:

```bash
KHAN_HOME=/tmp/khan-state khan init
```

Example:

```yaml
global:
  codex_bin: codex
  cursor_agent_bin: cursor-agent
  state_dir: ~/.khan
  default_profile: default
  max_concurrent_runs: 3
notifications:
  input_needed: true
  say_bin: say
  phrase: Khan needs your input.
profiles:
  default:
    max_iterations: 4
    max_review_rounds: 2
    max_validation_retries: 2
    max_runtime_minutes: 45
    idle_timeout_minutes: 10
    checkpoint_before_run: true
    checkpoint_on_success: false
    auto_review: true
projects: {}
prompts: {}
```

When a task run enters `needs_human`, Khan calls `say` with the configured
phrase and run summary if `notifications.input_needed` is enabled and the binary
is available. Notification failures are recorded and do not fail the run.

## Architecture

Khan has three execution concepts.

Task runs are durable Codex loops. A task has a title, objective, success
criteria, profile, and persisted capsule. A run invokes Codex, captures JSONL
output, detects changed files from git, enforces capsule guardrails, runs
validation commands, optionally runs Codex review, and either succeeds, retries,
fails, or stops for human input.

`khan ask` is the shortest path into that same machinery: it accepts a
configured project name or local git path, creates a project config when needed,
infers validation commands, writes a task capsule, and either runs immediately
or queues the task.

Agent sessions are provider-neutral one-shot headless executions. A session uses
an adapter to build a command, parse stdout/stderr events, extract an external
session ID where possible, and summarize final state. The same session store
works for Codex, Cursor Agent, and custom adapters.

Duels are parent orchestration records that launch multiple provider-neutral
sessions against the same task in isolated git worktrees. Khan captures each
participant's transcript, changed files, diff stat, validation result, runtime,
summary, risks, and a generated `duel-report.md` for operator comparison.

The current orchestration direction intentionally follows the practical shape of
modern agent-first workflows: local control and extensibility inspired by Peter
Steinberger's OpenClaw work, many observable concurrent sessions and loops
popularized by Boris Cherny's Claude Code usage, and an agent cockpit where the
human compares outputs instead of manually juggling terminal tabs.

Important implementation areas:

- CLI command surface.
- Zero-friction ask intake for broad local tasks.
- Codex task loop.
- Provider-neutral session runner.
- Agent adapter protocol and built-ins.
- Provider duel runner and evidence reports.
- Cross-review runner and critique artifacts.
- Adopt/reject workflow for applying selected agent work.
- Durable queue, foreground worker, and detached daemon supervisor.
- Attention cards and metrics.
- SQLite migrations and persistence.
- Textual operator console scaffold.

## Agent Adapters

Built-ins:

- `CodexAgentAdapter` runs `codex exec --json`.
- `CursorAgentAdapter` runs `cursor-agent --print --output-format stream-json`.

To add another provider, implement the `AgentAdapter` protocol and register it
with `register_agent_adapter(...)`. The runner handles persistence, workspaces,
process groups, cancellation, stdout/stderr event capture, and status updates.

## Docs

- [Overview](docs/overview.md)
- [CLI](docs/cli.md)
- [Configuration](docs/configuration.md)
- [Agent Adapters](docs/agent-adapters.md)
- [Operations](docs/operations.md)

## Current Limits

- The task loop still uses Codex as its worker/reviewer engine.
- Agent sessions are headless one-shot sessions; same-session steering is not
  implemented yet.
- Duels, cross-review, and manual adopt/reject are implemented, but replay and
  benchmark workflows are still roadmap items.
- The TUI is an operator-console scaffold, not a full interactive platform.
- Detached daemon crash restart policy and daemon log tailing are not complete.
- No Python packaging metadata yet.

## Contributing

Keep provider-specific behavior in adapters. Keep orchestration state in the
store. Add tests with fake binaries rather than calling real external agents.

Before sending changes:

```bash
make test
```

## License

No license file is present yet. Add one before distributing Khan as a public
FOSS project.

## Legacy Entrypoint

The CLI wrapper is `khan`. The internal Python entrypoint remains:

```bash
python scripts/khan_cli.py ...
```

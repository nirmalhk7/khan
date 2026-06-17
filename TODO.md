# Khan Handoff

This file tracks remaining platform work. It is kept because not every backlog
item is implemented yet.

## Implemented

- Versioned SQLite migrations, including legacy-schema migration.
- Durable tasks, task capsules, task runs, agent sessions, events, artifacts,
  process IDs, heartbeats, commands, failure fingerprints, and external session
  IDs.
- Codex task loop with structured output, JSONL capture, validation, optional
  review, runtime timeout, idle timeout, pause/resume/cancel polling, retry
  limits, repeated-failure detection, empty-diff stop, and protected-path stop.
- Task capsules with acceptance criteria, expected files, allowed paths,
  protected paths, verification recipes, blast radius, dependencies, and
  conflict domains.
- Conflict-domain scheduling guard for active task runs.
- Capsule allowed-path enforcement after worker changes.
- Durable queue items for task runs and agent sessions.
- Foreground queue worker and daemon loop with success/failure recording.
- Worktree-by-default isolation and failed-worktree cleanup.
- Provider-neutral agent sessions with built-in `codex` and `cursor-agent`
  adapters.
- Adapter registry for future agent providers.
- macOS `say` notification when task runs enter `needs_human`.
- Attention router and JSON metrics command.
- CLI help text for projects, tasks, runs, sessions, attention, metrics, and
  TUI.
- README and `/docs` covering setup, CLI, config, adapters, and operations.
- Test coverage with fake Codex, Cursor Agent, and `say` binaries.

## Remaining

### Detached Daemon Supervision

- Queue dispatch exists through `khan queue work` and `khan daemon`, but both are
  foreground loops.
- Need detached process supervision, crash recovery, restart behavior, and stale
  lease reclamation policy.

### Same-Session Steering

- Provider-neutral sessions persist external IDs but do not yet resume or steer
  the same provider session.
- `RunCommand` supports `steer` conceptually, but no steering path injects new
  instructions into Codex or Cursor Agent sessions.
- `retry_run` still starts a fresh task run from the stored task and capsule.

### Multi-Agent Topology

- The durable task loop still has one Codex worker and one optional Codex
  reviewer.
- No supervisor, builder, tester, reviewer, summarizer, or parallel worker
  topology exists yet.
- No fan-out/fan-in protocol or per-role prompt templates exist yet.

### Production TUI

- The TUI is still a scaffold.
- Missing: row selection, detail panes, live log tailing, artifact drill-down,
  status coloring/filtering, and keybound run/session actions.

### Atomic Commit Enforcement

- Khan records changed files and artifacts but does not yet enforce commits on
  success.
- Need optional commit creation, commit-message policy, and clean-worktree
  enforcement before marking runs complete.

### Review Loop

- Reviewer verdict parsing is intentionally minimal.
- No accepted/rejected finding filter.
- No targeted validation rerun selection from review findings.
- No parallel validation plus review closeout.

### Project Discovery

- Discovery is still heuristic.
- Missing richer support for `justfile`, `tox.ini`, `uv`, monorepo script
  resolution, and more language-specific validation inference.

### Packaging And CI

- No Python package metadata yet.
- No shell completion wiring.
- No CI workflow yet.
- No license file yet.

## Recommended Next Steps

1. Add detached daemon supervision and stale lease recovery.
2. Add same-session steering for adapters that support resume/continue.
3. Upgrade the TUI into a real operator console.
4. Add optional atomic commit enforcement on successful runs.
5. Add packaging metadata, license, and CI.

## Useful Files

- `README.md`
- `docs/`
- `scripts/khan_cli.py`
- `scripts/khan/`
- `scripts/`

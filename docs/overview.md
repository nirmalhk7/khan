# Khan Overview

Khan is a local-first orchestration layer for coding agents. The goal is to make
agent work observable and controllable from one platform instead of scattering
state across terminal tabs, worktrees, and provider-specific session stores.

## Design Principles

- One control plane: projects, task runs, direct agent sessions, logs, artifacts,
  and process state are stored in one SQLite database.
- Adapter-backed providers: Codex and Cursor Agent are built-in adapters, and
  future agents should plug in through the same interface.
- Isolated workspaces: Khan can use git worktrees when a project should not be
  mutated in place or when multiple sessions operate on the same project.
- Human attention is explicit: unsafe or ambiguous states move runs to
  `needs_human`, record why, and optionally trigger macOS `say`.
- Durable evidence: prompts, validation results, review outputs, JSONL streams,
  process IDs, and session IDs are recorded for later inspection.

## Current Architecture

The repository is a Python CLI application under `scripts/`.

- `cli.py`: Typer CLI and command grouping.
- `loop_engine.py`: Codex task loop, validation, review, retry, and stop logic.
- `agents.py`: provider-neutral session runner and process lifecycle.
- `agent_adapters.py`: adapter protocol and built-in `codex` / `cursor-agent`
  implementations.
- `store.py`: SQLite migrations and persistence APIs.
- `config.py`: config loading, defaults, project discovery, and validation
  command inference.
- `tui.py`: Textual operator-console scaffold.
- `notifier.py`: input-needed notification channel using macOS `say`.
- `worktree.py`: git worktree creation and cleanup.
- `validator.py`: project validation command runner.

## Runs vs Sessions

Khan has two execution concepts.

Task runs are durable Codex loops. A task has a title, objective, success
criteria, and optional profile. A run invokes Codex, captures JSONL output,
detects changed files from git, runs validation commands, optionally runs Codex
review, and either succeeds, retries, fails, or stops for human input.

Agent sessions are provider-neutral one-shot headless executions. A session uses
an adapter to build a command, parse stdout/stderr events, extract an external
session ID where possible, and summarize the final state. The same session store
works for Codex, Cursor Agent, and custom adapters.

## State Directory

By default, state is stored under `~/.khan`.

Important contents:

- `config.yaml`: user configuration.
- `orch.db`: SQLite state database.
- `runs/<id>/`: prompt, validation, review, summary, and JSONL artifacts.
- `schemas/<id>.json`: JSON schemas used for Codex structured output.
- `worktrees/<project>/<id>/`: managed git worktrees.
- `locks/`: file locks for scheduler and run/session operations.

## Referenced Orchestration Ideas

The current direction follows the same practical shape as modern agent
orchestration work:

- Local control plane over provider-specific terminals.
- Independent sessions that can be observed and cancelled.
- Worktree isolation for parallel or risky work.
- Event streams and artifacts as first-class operational evidence.
- Human attention routing instead of silent failure.
- Provider adapters instead of hard-coded agent assumptions.
- Durable queue items plus a foreground worker loop.

## Not Yet Complete

- No detached daemon supervisor yet.
- No same-session steering or resume path for the provider-neutral session API.
- No supervisor/builder/tester/reviewer topology yet.
- The TUI does not yet support selecting records, tailing logs, or triggering
  actions from keybindings.
- The task loop still uses Codex directly rather than the adapter abstraction.

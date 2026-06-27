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

The repository is a Python CLI application under `src/`.

- `cli.py`: Typer CLI and command grouping.
- `ask.py`: zero-friction task intake for broad prompts against configured
  projects or local git paths, with immediate evidence-ledger output for
  direct runs.
- `inspection.py`: operator-facing record lookup, evidence, and diff rendering.
- `orchestration.py`: relay, replay, steer, and benchmark flows built on the
  existing session runner, including named relay presets and replay metadata.
- `loop_engine.py`: Codex task loop, validation, review, retry, and stop logic.
- `agents.py`: provider-neutral session runner and process lifecycle.
- `agent_adapters.py`: adapter protocol and built-in `codex` / `cursor-agent`
  implementations.
- `duel.py`: provider duel orchestration, isolated candidate worktrees, task
  capsule artifacts, and comparison reports.
- `cross_review.py`: provider cross-review over duel participant diffs with
  parsed verdict fields and operator-facing decision cards.
- `adoption.py`: adopt/reject decisions, dirty-destination checks, and worktree
  cleanup.
- `store.py`: SQLite migrations and persistence APIs.
- `config.py`: config loading, defaults, project discovery, and validation
  command inference.
- `tui.py`: Textual operator-console cockpit.
- `notifier.py`: input-needed notification channel using macOS `say`.
- `worktree.py`: git worktree creation and cleanup.
- `validator.py`: project validation command runner.

## Runs, Sessions, And Duels

Khan has four execution concepts.

Pipelines are the primary product path. `khan ask` creates a task capsule,
runs a Codex planning session, launches Codex and Cursor Agent as independent
builders through the duel runner, cross-reviews successful candidates, scores
the evidence, and writes `pipeline-report.md` plus `decision-card.md`. Pipelines
enter `awaiting_decision`; Khan does not auto-adopt.

Task runs are durable Codex loops. A task has a title, objective, success
criteria, and optional profile. A run invokes Codex, captures JSONL output,
detects changed files from git, runs validation commands, optionally runs Codex
review, and either succeeds, retries, fails, or stops for human input.

`khan ask --mode single` keeps the original task-run behavior. `khan ask
--mode queue` queues the default pipeline payload for a worker.

Agent sessions are provider-neutral one-shot headless executions. A session uses
an adapter to build a command, parse stdout/stderr events, extract an external
session ID where possible, and summarize the final state. The same session store
works for Codex, Cursor Agent, and custom adapters.

Duels are parent orchestration records. A duel runs selected providers against
the same task prompt in isolated git worktrees, stores one participant per
provider, captures transcript events, changed files, diff stats, validation
results, runtime, summary, open risks, a task-capsule artifact, and writes a
`duel-report.md` artifact.
Completed duels enter `awaiting_decision` so the operator can compare and choose
which candidate to adopt.

Adoption decisions are durable operator actions. `khan adopt` copies selected
changes from a recorded run, session, or duel participant workspace into the
project checkout after a dirty-worktree safety check. `khan reject` records that
a candidate was declined and can clean up the candidate worktree.

Cross-reviews are second-order duel evidence. `khan cross-review <duel-id>` has
each completed provider review the other provider's diff using the same project
review prompt, then stores critique artifacts, parsed verdict fields, and a
cross-review report.

Operator inspection and orchestration are first-class too. `khan inbox`,
`khan show`, `khan last`, `khan get`, `khan summary`, `khan diff`, and
`khan explain` expose the stored evidence. `khan relay`, `khan steer`,
`khan replay`, and `khan bench` build on the same session and run store to
support longer workflows.

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
- Durable queue items plus foreground and detached daemon worker loops.
- Side-by-side provider comparison, because modern agent-first workflows are
  about supervising multiple candidates rather than trusting one terminal.
- Explicit adoption decisions so agent output is not silently merged.
- Cross-provider review so one agent's output is not trusted without an
  independent critique.

## Not Yet Complete

- The task loop still uses Codex directly rather than the adapter abstraction.

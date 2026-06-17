# Khan TODO

Khan is a local-first orchestration layer for Codex and Cursor Agent. It should
optimize for a developer actively supervising local coding work: fast task setup,
parallel provider comparison, strong evidence capture, safe adoption of changes,
and recoverable agent sessions.

## Implemented

- Versioned SQLite migrations and durable local state.
- Durable tasks, task capsules, task runs, agent sessions, events, artifacts,
  process IDs, heartbeats, commands, failure fingerprints, and external session
  IDs.
- Codex task loop with structured output, JSONL capture, validation, optional
  review, runtime timeout, idle timeout, pause/resume/cancel polling, retry
  limits, repeated-failure detection, empty-diff stop, allowed-path enforcement,
  and protected-path stop.
- Task capsules with acceptance criteria, expected files, allowed paths,
  protected paths, verification recipes, blast radius, dependencies, and conflict
  domains.
- Conflict-domain scheduling guard for active task runs.
- Durable queue items for task runs and agent sessions.
- Foreground queue worker, detached daemon supervisor, daemon heartbeat/status,
  stale lease recovery, and success/failure recording.
- Worktree-by-default isolation and failed-worktree cleanup.
- Provider-neutral agent sessions with built-in `codex` and `cursor-agent`
  adapters.
- Adapter registry for future providers.
- macOS `say` notification when task runs enter `needs_human`.
- Attention router and JSON metrics command.
- CLI docs, README, configuration docs, adapter docs, and operations docs.
- Test coverage with fake Codex, Cursor Agent, and `say` binaries.

## Khan-Unique Roadmap

### 1. Zero-Friction Local Tasks

- Add `khan ask . "prompt"` for current-repo one-off work without requiring
  `khan project add`.
- Auto-create an ephemeral project config from the current git root.
- Infer validation commands from the repo and persist the inferred recipe in the
  run artifacts.
- Add `khan last`, `khan ps`, `khan diff <id>`, `khan summary <id>`, and
  unambiguous partial ID resolution.

### 2. Provider Duel Mode

- Add `khan duel . "prompt"` to run Codex and Cursor Agent against the same task
  capsule in isolated sibling worktrees.
- Store one parent duel record and one child run/session per provider.
- Capture for each provider:
  - prompt
  - transcript
  - changed files
  - diff stat
  - validation result
  - runtime
  - summary
  - open risks
- Produce `duel-report.md` comparing both outputs.
- Add `khan duel show <id>` and `khan duel artifacts <id>`.

### 3. Cross-Review Mode

- Add `khan cross-review <duel-id>`.
- Have Codex review Cursor Agent's diff.
- Have Cursor Agent review Codex's diff using the same review prompt.
- Store both critiques as artifacts.
- Add a final Khan decision card with:
  - strongest implementation
  - validation winner
  - reviewer disagreements
  - files requiring human inspection
  - recommended adopt/reject action

### 4. Relay Mode

- Add `khan relay . "prompt" --first codex --second cursor-agent`.
- First provider plans or implements.
- Second provider continues from the first provider's workspace or critique.
- Support preset relays:
  - `codex-plan cursor-build`
  - `cursor-build codex-review`
  - `codex-fix cursor-polish`
- Persist every handoff prompt as an artifact so relays are inspectable.

### 5. Patch Adoption Workflow

- Add `khan adopt <run-id>` to copy a selected run's worktree changes into the
  main checkout.
- Add `khan reject <run-id>` to discard the worktree and mark the result rejected.
- Add `khan adopt <duel-id> --provider codex|cursor-agent`.
- Before adoption:
  - show changed files
  - show validation status
  - warn on protected paths
  - refuse adoption if the destination worktree is dirty unless `--force`
- Record adoption decisions in SQLite.

### 6. Replay And Benchmarking

- Add `khan replay <run-id> --provider codex|cursor-agent`.
- Reuse the original task capsule, prompt, validation recipe, and project state.
- Add `khan bench prompts.yaml` for repeatable provider evaluation.
- Score each run using:
  - validation pass/fail
  - number of iterations
  - review verdict
  - protected/allowed path compliance
  - human adoption decision
  - runtime
- Export benchmark results as JSON and Markdown.

### 7. Same-Session Steering

- Add `khan steer <session-id> "message"` for provider sessions where resume or
  continued input is supported.
- Add adapter methods:
  - `resume_command`
  - `send_message`
  - `supports_steering`
- Codex steering should use captured external session IDs when possible.
- Cursor Agent steering should use its external chat/session ID when possible.
- If a provider cannot steer, Khan should create a continuation session with the
  prior transcript and mark it as a fork.

### 8. Evidence Ledger

- Add a run-level `evidence.md` artifact summarizing:
  - objective
  - capsule constraints
  - provider
  - commands run
  - files changed
  - validation output
  - review output
  - risks
  - final recommendation
- Add `khan explain <id>` to render this evidence in the terminal.
- Add JSON output for scripts: `khan explain <id> --json`.

### 9. TUI As Local Cockpit

- Upgrade the Textual TUI around local decision-making, not fleet PR monitoring.
- Add panes for:
  - active runs
  - duels
  - provider sessions
  - evidence
  - diff summary
  - validation output
- Add keybindings:
  - `d` diff
  - `e` evidence
  - `a` adopt
  - `x` reject
  - `r` replay
  - `s` steer
  - `c` cancel
- Show Codex and Cursor Agent outputs side-by-side for duel records.

### 10. Detached Supervision Polish

- Add automatic crash restart policy for failed daemon records.
- Add `khan daemon logs`.
- Add launchd/systemd templates for users who want OS-level supervision.
- Add stale daemon heartbeat detection thresholds in config.

## Test Plan

- CLI tests for `ask`, partial IDs, `duel`, `cross-review`, `relay`, `adopt`,
  `reject`, `replay`, `bench`, and `explain`.
- Store migration tests for duel records, adoption decisions, replay metadata,
  and evidence artifacts.
- Fake Codex and fake Cursor Agent tests for provider duel and cross-review.
- Worktree tests for safe adoption, dirty-destination refusal, and rejection
  cleanup.
- Adapter tests for steering support and continuation fallback.
- TUI smoke tests for duel view, evidence view, and action keybindings.

## Provider Constraint

Codex and Cursor Agent are the supported providers for now.

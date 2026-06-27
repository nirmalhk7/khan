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

## Daily Task Intake

For broad local work, start with `ask`:

```bash
khan ask . "Implement the feature and run the inferred checks."
```

This creates or reuses a project config for the current git repository, infers
validation commands from repo files, persists a task capsule, and runs the
default multi-agent pipeline. Codex plans, Codex and Cursor Agent build in
isolated worktrees, cross-review runs when both builders finish, and the result
is an explicit decision card. To hand the same pipeline to a queue worker or
detached daemon instead:

```bash
khan ask . "Implement the feature and run the inferred checks." --mode queue
khan daemon start
```

Use explicit constraints when the task has a known contract:

```bash
khan ask . "Update the CLI docs." \
  --accept "README and docs/cli.md mention the command." \
  --verify "make test"
```

When `ask` runs immediately, it prints the evidence ledger after the status
line so the pipeline report, decision card, and artifacts are visible in the
same terminal. Use `--mode single` for the original one-agent Codex loop.

## Inspect And Explain

When a run, session, duel, or adoption needs review:

```bash
khan inbox
khan show <id>
khan last --kind run
khan get
khan summary <id>
khan diff <id>
khan explain <id>
khan explain <id> --json
```

`inbox` is the actionable view, `show` is the human-readable decision/evidence
view, and `summary` is the compact snapshot. `explain` is the evidence ledger
and is what you want when you need structured output or to reconstruct why Khan
made a decision.

## Relay, Replay, And Steer

Use these when you want the model to carry state forward explicitly:

```bash
khan relay . "Implement the feature in two steps." --preset "codex-plan cursor-build"
khan steer <session-id> "Continue from the last transcript."
khan replay <run-id> --provider codex
khan replay <run-id> --provider cursor-agent
khan bench prompts.yaml
```

Relay persists the handoff prompt, steer keeps the same session lineage where it
can, replay reuses the original task and project context, and bench runs repeat
items from a YAML prompt list.

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

Duel artifacts include:

- `duel-report.md`
- `<provider>-result.md`

Cross-review artifacts include:

- `cross-review-report.md`
- `<reviewer>-reviews-<subject>.md`

Participant artifacts include the prompt context, session ID, workspace,
summary, changed files, diff stat, validation status, open risks, and transcript
events captured from the adapter stream.

## Provider Duels

Run Codex and Cursor Agent against the same broad task:

```bash
khan duel run <project-or-path> "Implement this feature and run tests."
```

Inspect the operator decision record:

```bash
khan duel show <duel-id>
khan duel artifacts <duel-id>
khan inbox
```

Duels always force isolated git worktrees for participants, even if the project
normally uses `workspace_mode: in_place`. Khan records untracked and modified
files from each workspace so brand-new files are visible in the report.

The duel is marked `awaiting_decision` when at least one participant completes.
Inspect the report and participant workspaces, then use `adopt` or `reject` to
record the operator decision.

## Cross-Review

Run both providers as reviewers over each other's candidate diff:

```bash
khan cross-review <duel-id>
```

Inspect the result:

```bash
khan cross-review-show <cross-review-id>
khan cross-review-artifacts <cross-review-id>
khan inbox
```

Cross-review uses the same configured project review prompt for every
reviewer/subject pair. Reviewers run as normal provider-neutral sessions in
isolated worktrees, so critiques are captured with the same stdout/stderr,
summary, external session ID, and artifact plumbing as regular sessions.
Khan also parses the reviewer verdict line into structured fields so operator
warnings can reflect which implementation looks strongest and whether human
inspection is still required.

## Adoption And Rejection

Adopt a selected candidate:

```bash
khan adopt <duel-id> --provider codex
```

Reject a candidate and remove its source worktree:

```bash
khan reject <duel-id> --provider cursor-agent
```

Inspect decision history:

```bash
khan adoption list
khan metrics
```

Adoption safety rules:

- Khan refuses to adopt into a dirty destination checkout unless `--force` is
  supplied.
- Khan refuses same-workspace adoption because there is nothing safe to copy.
- Candidate paths are checked for absolute paths, `..`, and `.git`.
- Protected path changes are called out in the recorded decision summary.
- Cross-review warnings are folded into the recorded decision summary when the
  source candidate has already been reviewed.
- Failed adoption attempts are recorded with `status: failed`.

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

`daemon logs` prints a compact text view of recent daemon records.

Use the same `--config <path>` with `daemon start`, `daemon status`, and
`daemon stop` when operating a non-default Khan state store.

Daemon health is driven by the configured `daemon.stale_heartbeat_seconds`
threshold. If `daemon.restart_on_crash` is enabled, Khan restarts a crashed
daemon with the same command and records the restart as a new daemon row.

For OS-level supervision, adapt the included templates:

- `deploy/systemd/khan.service`
- `deploy/launchd/com.khan.khan.plist`

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
- zero-friction `ask` project auto-discovery, pipeline mode, single mode, and queue mode
- operator inspection commands (`inbox`, `show`, `last`, `get`, `summary`, `diff`, `explain`)
- relay, replay, bench, and steer command flows
- provider duel records, reports, and worktree isolation
- cross-review records, critique artifacts, and report artifacts
- adopt/reject decisions, dirty-destination refusal, and rejection cleanup
- adoption validation and commit creation
- agent session persistence
- built-in Codex and Cursor Agent sessions
- custom adapter registration
- input-needed notification through fake `say`

## Known Operational Gaps

- No web UI.

# Configuration

Khan loads YAML config from `~/.khan/config.yaml` by default. If the home
state directory is not writable, it falls back to repo-local `.khan`.

Set `KHAN_HOME` to force a state directory:

```bash
KHAN_HOME=/tmp/khan-state khan init
```

## Global Settings

```yaml
global:
  codex_bin: codex
  cursor_agent_bin: cursor-agent
  state_dir: ~/.khan
  default_profile: default
  max_concurrent_runs: 3
```

- `codex_bin`: binary used by the Codex task loop and Codex adapter.
- `cursor_agent_bin`: binary used by the Cursor Agent adapter.
- `state_dir`: database, artifacts, locks, schemas, and worktrees.
- `default_profile`: profile used when a task/project does not specify one.
- `max_concurrent_runs`: combined limit for active task runs and agent sessions.

## Notifications

```yaml
notifications:
  input_needed: true
  say_bin: say
  phrase: Khan needs your input.
```

When a task run enters `needs_human`, Khan resolves `say_bin` with `PATH` and
calls it with the phrase, run ID prefix, and summary. If `say` is missing,
disabled, or fails, Khan records a skipped notification and continues.

## Profiles

```yaml
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
    require_human_approval_after: 2
    stop_on_repeated_failure: true
    stop_on_empty_diff: true
```

Profiles control loop behavior for Codex task runs.

## Projects

```yaml
projects:
  khan:
    name: khan
    path: /Users/nirmalhk7/Documents/DevWorld/khan
    default_branch: main
    sandbox: workspace-write
    approval_policy: on-request
    workspace_mode: auto
    validate_commands:
      - make test
    build_commands: []
    protected_paths:
      - .env
      - secrets
    skills_hint: []
    env: {}
```

- `workspace_mode`: `auto`, `worktree`, or `in_place`.
- `protected_paths`: changed files under these paths stop the run with
  `needs_human`.
- `env`: provider process environment overrides.
- `validate_commands`: shell commands run after worker changes.

## Discovery

`khan project add` uses heuristics:

- `package.json`: common npm scripts such as `test`, `lint`, `build`, `check`.
- `pyproject.toml`: pytest-related commands.
- `Cargo.toml`: `cargo test` and `cargo check`.
- `Makefile`: targets such as `test`, `lint`, `check`, `build`.

Review inferred commands before trusting them for a project.

## Task Capsules

Task capsules are stored in SQLite with each task. They can be created from
`khan task create` flags:

```bash
khan task create khan \
  --title "Scoped change" \
  --prompt "Update docs." \
  --success "Docs are accurate." \
  --accept "README is updated." \
  --allowed-path README.md \
  --verify "make test" \
  --conflict-domain docs
```

Capsule fields:

- `objective`: task objective.
- `acceptance_criteria`: explicit done conditions.
- `expected_files`: files expected to change.
- `allowed_paths`: paths the worker may edit before Khan stops for input.
- `protected_paths`: extra protected paths beyond the project config.
- `verification`: commands or checks expected for completion.
- `blast_radius`: `small`, `medium`, or `large`.
- `dependencies`: prerequisite notes.
- `conflict_domains`: scheduling domains that cannot overlap with active runs.

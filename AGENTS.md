# AGENTS.md

Guidance for coding agents working in this repository.

## Project Summary

Khan is a Python CLI for local multi-agent coding workflows. The primary user
experience is:

```bash
khan ask . "..."
khan inbox
khan show <id>
khan adopt <id> --provider <provider>
```

`khan ask` defaults to a pipeline: plan, build with multiple providers,
cross-review, then present a decision card. Khan must not auto-adopt generated
changes.

## Startup Checklist

Before editing:

1. Read this file and any more specific `AGENTS.md` or `AGENTS.override.md`.
2. Check the worktree:

   ```bash
   git status --short
   ```

3. Inspect the relevant files before changing them.
4. Preserve user changes. Do not revert unrelated modifications.

## Repository Layout

```text
khan                  executable launcher
src/khan_cli.py       module entrypoint
src/khan_core/        CLI, storage, runners, adapters, TUI, orchestration
src/khan/             compatibility package
tests/                unittest suite
docs/                 user/operator documentation
deploy/               service manager examples
```

Core modules:

- `src/khan_core/cli.py`: Typer CLI commands.
- `src/khan_core/models.py`: Pydantic records and status literals.
- `src/khan_core/store.py`: SQLite migrations and persistence API.
- `src/khan_core/pipeline.py`: pipeline runner.
- `src/khan_core/ask.py`: `khan ask` intake.
- `src/khan_core/duel.py`: multi-provider build comparison.
- `src/khan_core/cross_review.py`: provider cross-review.
- `src/khan_core/adoption.py`: adopt/reject workflows.
- `src/khan_core/attention.py`: inbox cards and metrics.
- `src/khan_core/inspection.py`: `show`, evidence, summary, and diff views.

## Commands

Use the existing project commands:

```bash
make setup
make test
```

`make test` runs:

```bash
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -v
```

For quick syntax checks:

```bash
python -m compileall src/khan_core
```

## Coding Rules

- Prefer existing patterns in `src/khan_core`.
- Keep storage changes behind typed `Store` methods.
- Add SQLite migrations by appending to `MIGRATIONS`; update migration tests.
- Keep records serializable through Pydantic `model_dump(mode="json")`.
- Keep CLI output human-readable and scriptable where existing commands already
  support JSON.
- Do not introduce network calls in tests.
- Do not add auto-adoption behavior.
- Do not hide failed validation or review findings from decision cards.

## CLI Product Contract

The public, primary commands are:

- `khan ask`
- `khan inbox`
- `khan show`
- `khan adopt`
- `khan reject`
- `khan tui`
- `khan doctor`

Advanced commands can remain available, but do not make root help revolve around
low-level runner internals.

`ask` modes:

- `pipeline`: default multi-agent decision pipeline.
- `single`: original one-agent Codex task loop.
- `queue`: queue the selected work for a worker.

`--enqueue` is a compatibility alias for queue behavior.

## Testing Expectations

Add or update tests for:

- CLI behavior and help text.
- SQLite migrations and legacy migration.
- Store persistence for new records.
- Runner behavior and failure paths.
- Attention cards, metrics, and inspection output.
- Adoption/rejection behavior when candidates are involved.

Prefer focused tests in `tests/test_foundation.py` using the existing fake Codex
and Cursor Agent binaries.

## Documentation Expectations

Update docs when changing user-visible behavior:

- `README.md` for top-level FOSS/project guidance.
- `docs/cli.md` for command reference.
- `docs/operations.md` for day-to-day workflows.
- `docs/configuration.md` for config fields.
- `docs/overview.md` for architecture.
- `docs/agent-adapters.md` for provider adapter changes.

## Safety Notes

- Treat generated worktrees and artifacts as potentially sensitive.
- Do not print secrets from config, prompts, or artifacts.
- Use `protected_paths` in examples when discussing sensitive files.
- Never perform destructive git operations unless the user explicitly requests
  them.

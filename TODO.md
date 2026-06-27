# TODO

## Implementation Plan

### 1. Full-access project policy

Goal: let Khan operate with broad local authority inside the repository by default, including package installation and other normal setup commands.

- Add an explicit project-level policy block in config, for example:

  ```yaml
  projects:
    khan:
      access_policy:
        filesystem: full
        shell: full
        package_install: allowed
        network: restricted
  ```

- Treat `package_install: allowed` as permission for common local package-manager commands such as `npm install`, `pip install`, `uv sync`, `cargo install`, or equivalent repo-local setup steps.
- Keep the policy scoped to the repository workspace. It should not imply host-wide access outside the repo or secret-bearing paths.
- If a project needs a narrower policy later, let it override the default at the project level instead of changing global behavior.

### 2. Web query allowlist

Goal: any web access should be explicitly constrained by a glob-based domain allowlist.

- Add a web policy field to config, for example:

  ```yaml
  global:
    web_allowed_domains:
      - "*.python.org"
      - "*.npmjs.com"
      - "docs.*.com"
  ```

- Match domains using glob semantics against the request host before any web fetch or browser action occurs.
- Reject or pause requests that do not match an allowed glob and record the denied host in the run/session evidence.
- Keep the allowlist auditable by showing the resolved glob list in `khan doctor`, `khan summary`, or a dedicated config inspection command.

### 3. Reduce instruction requests

Goal: stop asking the operator for the same safe defaults repeatedly.

- Move reusable defaults into config first:
  - allowed package managers
  - trusted web domain globs
  - preferred validation commands
  - default review prompt text
- Teach task creation to inherit project policy automatically so `ask` and `task create` only ask for exceptions.
- Add a prompt-builder rule that includes the active policy in the task prompt only once, so the agent sees it up front and does not re-request it mid-run.
- Prefer a deny-by-exception model: if something is not in the config policy, Khan should ask once, store the exception, and reuse it for the same project/task shape.

### 4. Code changes this implies

- `src/khan_core/models.py`
  - add an access-policy model and a web-allowlist field
  - keep defaults conservative enough that existing configs still load
- `src/khan_core/config.py`
  - load and persist the new config fields
  - ensure defaults are written to `DEFAULT_CONFIG`
- `src/khan_core/prompt_builder.py`
  - inject the active policy into the worker prompt
- `src/khan_core/loop_engine.py`
  - apply the policy before any web-querying step or package-install step
  - record denied operations as events
- `src/khan_core/cli.py`
  - expose the new policy in help text or inspection output
- `docs/configuration.md`
  - document the policy schema and examples
- `docs/cli.md` / `docs/operations.md`
  - document how the policy reduces instruction churn in day-to-day use
- `tests/test_foundation.py`
  - cover config loading/saving, allowlist matching, denied web access, and package-install-allowed paths

### 5. Acceptance criteria

- A configured project can run without repeated permission prompts for normal local setup work.
- Web access is blocked unless the host matches one of the configured glob patterns.
- The allowlist and access policy are visible in config/docs and are covered by tests.
- Existing configs continue to load without migration breakage.

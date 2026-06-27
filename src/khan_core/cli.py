from __future__ import annotations

import json
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .adoption import AdoptionError, AdoptionManager
from .agent_adapters import agent_adapter_names
from .agents import AgentSessionError, AgentSessionRunner
from .ask import AskError, AskRunner
from .attention import AttentionRouter
from .config import discover_project, load_config, save_config, write_default_config
from .cross_review import CrossReviewError, CrossReviewRunner
from .daemon import DaemonSupervisor, DaemonSupervisorError
from .doctor import run_doctor
from .duel import DuelError, DuelRunner
from .loop_engine import LoopEngine
from .models import AgentProvider, TaskCapsule
from .queue_worker import QueueWorker
from .store import Store

app = typer.Typer(
    no_args_is_help=True,
    help="Khan orchestrates, records, and monitors local coding agents from one control plane.",
)
project_app = typer.Typer(no_args_is_help=True, help="Register and inspect repositories Khan can operate on.")
task_app = typer.Typer(no_args_is_help=True, help="Create and run durable Codex task loops with validation.")
run_app = typer.Typer(no_args_is_help=True, help="Inspect and control task-loop runs.")
session_app = typer.Typer(no_args_is_help=True, help="Start and inspect provider-neutral agent sessions.")
queue_app = typer.Typer(no_args_is_help=True, help="Manage Khan's durable work queue.")
daemon_app = typer.Typer(no_args_is_help=True, help="Manage Khan's detached daemon supervisor.")
duel_app = typer.Typer(
    no_args_is_help=True,
    help='Run and inspect provider duels. Use `khan duel run . "prompt"` for local path targets.',
)
adoption_app = typer.Typer(no_args_is_help=True, help="Inspect adoption and rejection decisions.")
app.add_typer(project_app, name="project")
app.add_typer(task_app, name="task")
app.add_typer(run_app, name="run")
app.add_typer(session_app, name="session")
app.add_typer(queue_app, name="queue")
app.add_typer(daemon_app, name="daemon")
app.add_typer(duel_app, name="duel")
app.add_typer(adoption_app, name="adoption")

console = Console()


@app.command()
def init() -> None:
    """Create the default config and SQLite state store."""
    config_path = write_default_config()
    config = load_config(config_path)
    Store(config.global_config.state_dir)
    console.print(f"Initialized Khan at {config_path}")


@app.command()
def doctor() -> None:
    """Check configured binaries, state storage, and registered projects."""
    config = load_config()
    table = Table(title="Khan doctor")
    table.add_column("Check")
    table.add_column("Value")
    for key, value in run_doctor(config):
        table.add_row(key, value)
    console.print(table)


@app.command()
def ask(
    target: str,
    prompt: str,
    title: str | None = typer.Option(None, "--title", help="Task title; defaults to the prompt prefix."),
    success: str | None = typer.Option(None, "--success", help="Success criterion for the generated task."),
    profile: str | None = typer.Option(None, "--profile", help="Loop profile to use."),
    accept: list[str] | None = typer.Option(None, "--accept", help="Acceptance criterion; repeatable."),
    verify: list[str] | None = typer.Option(None, "--verify", help="Verification command; repeatable."),
    enqueue: bool = typer.Option(False, "--enqueue", help="Create and queue the task instead of running immediately."),
    priority: int = typer.Option(100, "--priority", help="Queue priority when using --enqueue."),
    config: Path | None = typer.Option(None, "--config", help="Config file path."),
) -> None:
    """Create and run a zero-friction local task from a broad prompt."""
    try:
        task, run, item = AskRunner(config).ask(
            target,
            prompt,
            title=title,
            success=success,
            profile=profile,
            accept=accept,
            verify=verify,
            enqueue=enqueue,
            priority=priority,
        )
    except AskError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if item:
        console.print(f"Created task {task.id} and queued item {item.id}")
        return
    assert run is not None
    console.print(f"Created task {task.id}; run {run.id} finished with status {run.status}")


@project_app.command("add")
def project_add(name: str, path: Path) -> None:
    """Register a project and infer basic validation commands."""
    config = load_config()
    project = discover_project(path.expanduser().resolve(), name)
    config.projects[name] = project
    save_config(config)
    console.print(f"Added project {name} -> {project.path}")


@project_app.command("list")
def project_list() -> None:
    """List registered projects."""
    config = load_config()
    table = Table(title="Projects")
    table.add_column("Name")
    table.add_column("Path")
    table.add_column("Branch")
    table.add_column("Validate")
    for name, project in config.projects.items():
        table.add_row(name, str(project.path), project.default_branch, ", ".join(project.validate_commands) or "-")
    console.print(table)


@task_app.command("create")
def task_create(
    project: str,
    title: str = typer.Option(..., "--title"),
    prompt: str = typer.Option(..., "--prompt"),
    success: str = typer.Option(..., "--success"),
    profile: str | None = typer.Option(None, "--profile"),
    accept: list[str] | None = typer.Option(None, "--accept", help="Acceptance criterion for the task capsule."),
    expected_file: list[str] | None = typer.Option(None, "--expected-file", help="File expected to change."),
    allowed_path: list[str] | None = typer.Option(None, "--allowed-path", help="Path the task is allowed to edit."),
    protected_path: list[str] | None = typer.Option(None, "--protected-path", help="Capsule-specific protected path."),
    verify: list[str] | None = typer.Option(None, "--verify", help="Verification command or check."),
    dependency: list[str] | None = typer.Option(None, "--dependency", help="Dependency or prerequisite."),
    conflict_domain: list[str] | None = typer.Option(None, "--conflict-domain", help="Conflict domain for scheduling."),
    blast_radius: str = typer.Option("small", "--blast-radius", help="small, medium, or large."),
) -> None:
    """Create a durable task for the Codex loop engine."""
    config = load_config()
    if project not in config.projects:
        raise typer.BadParameter(f"Project not found: {project}")
    if blast_radius not in {"small", "medium", "large"}:
        raise typer.BadParameter("blast-radius must be one of: small, medium, large")
    capsule = TaskCapsule(
        objective=prompt,
        acceptance_criteria=accept or [success],
        expected_files=expected_file or [],
        allowed_paths=allowed_path or [],
        protected_paths=protected_path or [],
        verification=verify or [],
        blast_radius=blast_radius,  # type: ignore[arg-type]
        dependencies=dependency or [],
        conflict_domains=conflict_domain or [],
    )
    store = Store(config.global_config.state_dir)
    task = store.create_task(project, title, prompt, success, profile, capsule)
    console.print(f"Created task {task.id}")


@task_app.command("capsule")
def task_capsule(task_id: str) -> None:
    """Print a task capsule as JSON."""
    config = load_config()
    capsule = Store(config.global_config.state_dir).get_task_capsule(task_id)
    console.print_json(capsule.model_dump_json())


@task_app.command("run")
def task_run(task_id: str) -> None:
    """Run a task through the Codex worker, validation, and optional review loop."""
    engine = LoopEngine()
    run_id = engine.run_task(task_id)
    run = engine.store.get_run(run_id)
    console.print(f"Run {run.id} finished with status {run.status}")


@task_app.command("enqueue")
def task_enqueue(task_id: str, priority: int = typer.Option(100, "--priority")) -> None:
    """Enqueue a task for daemon/worker execution."""
    config = load_config()
    item = Store(config.global_config.state_dir).enqueue_task(task_id, priority=priority)
    console.print(f"Queued task {task_id} as queue item {item.id}")


@task_app.command("retry")
def task_retry(run_id: str) -> None:
    """Create a fresh run from an existing run's task."""
    engine = LoopEngine()
    next_run = engine.retry_run(run_id)
    run = engine.store.get_run(next_run)
    console.print(f"Retry run {run.id} finished with status {run.status}")


@run_app.command("status")
def run_status(run_id: str) -> None:
    """Print a run record as JSON."""
    config = load_config()
    run = Store(config.global_config.state_dir).get_run(run_id)
    console.print_json(run.model_dump_json())


@run_app.command("logs")
def run_logs(run_id: str, limit: int = typer.Option(100, "--limit")) -> None:
    """Print recent run lifecycle events."""
    config = load_config()
    store = Store(config.global_config.state_dir)
    for event in store.list_events(run_id, limit=limit):
        console.print(f"{event.ts.isoformat()} [{event.phase}] {event.message}")


@run_app.command("artifacts")
def run_artifacts(run_id: str) -> None:
    """List artifact paths captured for a run."""
    config = load_config()
    for path in Store(config.global_config.state_dir).list_artifacts(run_id):
        console.print(str(path))


def _queue_control(run_id: str, command: str) -> None:
    config = load_config()
    store = Store(config.global_config.state_dir)
    run = store.get_run(run_id)
    if run.status not in {"running", "paused", "stopping"}:
        raise typer.BadParameter(f"Run {run_id} is not controllable in status {run.status}")
    store.enqueue_command(run_id, command)
    status = {"pause": "paused", "resume": "running", "cancel": "stopping"}[command]
    store.update_run(run_id, status, f"{command.capitalize()} requested.")
    console.print(f"Queued {command} for run {run_id}")


@run_app.command("pause")
def run_pause(run_id: str) -> None:
    """Request SIGSTOP-style pause for a running Codex task process."""
    _queue_control(run_id, "pause")


@run_app.command("resume")
def run_resume(run_id: str) -> None:
    """Resume a paused Codex task process."""
    _queue_control(run_id, "resume")


@run_app.command("cancel")
def run_cancel(run_id: str) -> None:
    """Request cancellation for a running Codex task process."""
    _queue_control(run_id, "cancel")


def _provider(value: str) -> AgentProvider:
    if value not in agent_adapter_names():
        raise typer.BadParameter(f"provider must be one of: {', '.join(agent_adapter_names())}")
    return value


@session_app.command("providers")
def session_providers() -> None:
    """List registered agent adapters."""
    table = Table(title="Agent Providers")
    table.add_column("Provider")
    for provider in agent_adapter_names():
        table.add_row(provider)
    console.print(table)


@session_app.command("start")
def session_start(
    provider: str,
    project: str,
    prompt: str = typer.Option(..., "--prompt"),
    worktree: bool = typer.Option(False, "--worktree", help="Force an isolated git worktree for this session."),
) -> None:
    """Start a one-shot headless session through a registered agent adapter."""
    runner = AgentSessionRunner()
    session_id = runner.start_session(_provider(provider), project, prompt, force_worktree=worktree)
    session = runner.store.get_agent_session(session_id)
    console.print(f"Session {session.id} finished with status {session.status}")


@session_app.command("enqueue")
def session_enqueue(
    provider: str,
    project: str,
    prompt: str = typer.Option(..., "--prompt"),
    worktree: bool = typer.Option(False, "--worktree", help="Force an isolated git worktree for this session."),
    priority: int = typer.Option(100, "--priority"),
) -> None:
    """Enqueue an agent session for daemon/worker execution."""
    config = load_config()
    item = Store(config.global_config.state_dir).enqueue_session(
        _provider(provider),
        project,
        prompt,
        force_worktree=worktree,
        priority=priority,
    )
    console.print(f"Queued {provider} session as queue item {item.id}")


@session_app.command("list")
def session_list(active: bool = typer.Option(False, "--active")) -> None:
    """List provider-neutral agent sessions."""
    config = load_config()
    store = Store(config.global_config.state_dir)
    table = Table(title="Agent Sessions")
    table.add_column("Session")
    table.add_column("Provider")
    table.add_column("Project")
    table.add_column("Status")
    table.add_column("Workspace")
    table.add_column("External")
    for session in store.list_agent_sessions(active_only=active):
        table.add_row(
            session.id[:8],
            session.provider,
            session.project,
            session.status,
            session.workspace,
            session.external_id or "-",
        )
    console.print(table)


@session_app.command("status")
def session_status(session_id: str) -> None:
    """Print an agent session record as JSON."""
    config = load_config()
    session = Store(config.global_config.state_dir).get_agent_session(session_id)
    console.print_json(session.model_dump_json())


@session_app.command("logs")
def session_logs(session_id: str, limit: int = typer.Option(100, "--limit")) -> None:
    """Print recent stdout, stderr, and system events for an agent session."""
    config = load_config()
    store = Store(config.global_config.state_dir)
    for event in store.list_agent_session_events(session_id, limit=limit):
        console.print(f"{event.ts.isoformat()} [{event.stream}] {event.message}")


@session_app.command("cancel")
def session_cancel(session_id: str) -> None:
    """Terminate an active agent session process group."""
    try:
        AgentSessionRunner().cancel_session(session_id)
    except AgentSessionError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"Cancel requested for session {session_id}")


def _run_duel(target: str, prompt: str, provider: list[str] | None, config: Path | None, validate: bool) -> None:
    try:
        duel = DuelRunner(config).run_duel(target, prompt, providers=provider or None, validate=validate)
    except DuelError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"Duel {duel.id} finished with status {duel.status}")
    if duel.report_path:
        console.print(f"Report: {duel.report_path}")


@duel_app.command("run")
def duel_run(
    target: str,
    prompt: str,
    provider: list[str] | None = typer.Option(None, "--provider", help="Provider to include; repeatable."),
    config: Path | None = typer.Option(None, "--config", help="Config file path."),
    validate: bool = typer.Option(True, "--validate/--no-validate", help="Run project validation for each candidate."),
) -> None:
    """Run Codex and Cursor Agent against the same task in isolated worktrees."""
    _run_duel(target, prompt, provider, config, validate)


@duel_app.command("list")
def duel_list(
    active: bool = typer.Option(False, "--active"),
    limit: int = typer.Option(50, "--limit"),
    config: Path | None = typer.Option(None, "--config", help="Config file path."),
) -> None:
    """List provider duel records."""
    loaded = load_config(config)
    store = Store(loaded.global_config.state_dir)
    table = Table(title="Duels")
    table.add_column("Duel")
    table.add_column("Project")
    table.add_column("Status")
    table.add_column("Providers")
    table.add_column("Summary")
    for duel_record in store.list_duels(active_only=active, limit=limit):
        table.add_row(
            duel_record.id[:8],
            duel_record.project,
            duel_record.status,
            ", ".join(duel_record.providers),
            duel_record.summary or "-",
        )
    console.print(table)


@duel_app.command("show")
def duel_show(
    duel_id: str,
    json_output: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
    config: Path | None = typer.Option(None, "--config", help="Config file path."),
) -> None:
    """Show a duel and its provider participants."""
    loaded = load_config(config)
    store = Store(loaded.global_config.state_dir)
    duel_record = store.get_duel(duel_id)
    participants = store.list_duel_participants(duel_id)
    if json_output:
        console.print_json(
            json.dumps(
                {
                    "duel": duel_record.model_dump(mode="json"),
                    "participants": [participant.model_dump(mode="json") for participant in participants],
                }
            )
        )
        return
    console.print(f"[bold]Duel[/bold] {duel_record.id}  [bold]Status[/bold] {duel_record.status}")
    console.print(duel_record.summary or "-")
    if duel_record.report_path:
        console.print(f"Report: {duel_record.report_path}")
    table = Table(title="Participants")
    table.add_column("Provider")
    table.add_column("Status")
    table.add_column("Session")
    table.add_column("Validation")
    table.add_column("Files")
    table.add_column("Runtime")
    table.add_column("Artifact")
    for participant in participants:
        validation = (
            "skipped" if participant.validation_ok is None else "pass" if participant.validation_ok else "fail"
        )
        table.add_row(
            participant.provider,
            participant.status,
            participant.session_id[:8] if participant.session_id else "-",
            validation,
            str(len(participant.changed_files)),
            f"{participant.runtime_seconds:.2f}s",
            participant.artifact_path or "-",
        )
    console.print(table)


@duel_app.command("artifacts")
def duel_artifacts(
    duel_id: str,
    config: Path | None = typer.Option(None, "--config", help="Config file path."),
) -> None:
    """List artifacts captured for a provider duel."""
    loaded = load_config(config)
    for path in Store(loaded.global_config.state_dir).list_artifacts(duel_id):
        console.print(str(path))


@app.command()
def adopt(
    target_id: str,
    provider: str | None = typer.Option(None, "--provider", help="Provider when adopting a duel participant."),
    force: bool = typer.Option(False, "--force", help="Allow adoption into a dirty destination worktree."),
    cleanup: bool = typer.Option(False, "--cleanup", help="Remove the source worktree after adoption."),
    config: Path | None = typer.Option(None, "--config", help="Config file path."),
) -> None:
    """Adopt changes from a run, session, or duel participant into the project checkout."""
    try:
        decision = AdoptionManager(config).adopt(target_id, provider=provider, force=force, cleanup=cleanup)
    except AdoptionError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"Adopted {decision.target_type} {decision.target_id} as decision {decision.id}")
    console.print(decision.summary)


@app.command()
def reject(
    target_id: str,
    provider: str | None = typer.Option(None, "--provider", help="Provider when rejecting a duel participant."),
    cleanup: bool = typer.Option(True, "--cleanup/--keep-worktree", help="Remove the source worktree after rejection."),
    config: Path | None = typer.Option(None, "--config", help="Config file path."),
) -> None:
    """Reject changes from a run, session, or duel participant."""
    try:
        decision = AdoptionManager(config).reject(target_id, provider=provider, cleanup=cleanup)
    except AdoptionError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"Rejected {decision.target_type} {decision.target_id} as decision {decision.id}")
    console.print(decision.summary)


@adoption_app.command("list")
def adoption_list(
    limit: int = typer.Option(50, "--limit"),
    config: Path | None = typer.Option(None, "--config", help="Config file path."),
) -> None:
    """List adoption and rejection decisions."""
    loaded = load_config(config)
    table = Table(title="Adoption Decisions")
    table.add_column("Decision")
    table.add_column("Status")
    table.add_column("Target")
    table.add_column("Provider")
    table.add_column("Project")
    table.add_column("Files")
    table.add_column("Summary")
    for decision in Store(loaded.global_config.state_dir).list_adoption_decisions(limit=limit):
        table.add_row(
            decision.id[:8],
            decision.status,
            f"{decision.target_type}:{decision.target_id[:8]}",
            decision.provider or "-",
            decision.project,
            str(len(decision.changed_files)),
            decision.error or decision.summary or "-",
        )
    console.print(table)


@app.command("cross-review")
def cross_review(
    duel_id: str,
    config: Path | None = typer.Option(None, "--config", help="Config file path."),
) -> None:
    """Run provider cross-review for a completed duel."""
    try:
        record = CrossReviewRunner(config).run_cross_review(duel_id)
    except CrossReviewError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"Cross-review {record.id} finished with status {record.status}")
    if record.report_path:
        console.print(f"Report: {record.report_path}")


@app.command("cross-review-list")
def cross_review_list(
    duel_id: str | None = typer.Option(None, "--duel-id", help="Filter by duel ID."),
    active: bool = typer.Option(False, "--active"),
    limit: int = typer.Option(50, "--limit"),
    config: Path | None = typer.Option(None, "--config", help="Config file path."),
) -> None:
    """List cross-review records."""
    loaded = load_config(config)
    table = Table(title="Cross-Reviews")
    table.add_column("CrossReview")
    table.add_column("Duel")
    table.add_column("Status")
    table.add_column("Summary")
    for record in Store(loaded.global_config.state_dir).list_cross_reviews(duel_id=duel_id, active_only=active, limit=limit):
        table.add_row(record.id[:8], record.duel_id[:8], record.status, record.summary or "-")
    console.print(table)


@app.command("cross-review-show")
def cross_review_show(
    cross_review_id: str,
    json_output: bool = typer.Option(False, "--json", help="Print JSON instead of a table."),
    config: Path | None = typer.Option(None, "--config", help="Config file path."),
) -> None:
    """Show a cross-review and its critiques."""
    loaded = load_config(config)
    store = Store(loaded.global_config.state_dir)
    record = store.get_cross_review(cross_review_id)
    critiques = store.list_cross_review_critiques(cross_review_id)
    if json_output:
        console.print_json(
            json.dumps(
                {
                    "cross_review": record.model_dump(mode="json"),
                    "critiques": [critique.model_dump(mode="json") for critique in critiques],
                }
            )
        )
        return
    console.print(f"[bold]Cross-review[/bold] {record.id}  [bold]Status[/bold] {record.status}")
    console.print(record.summary or "-")
    if record.report_path:
        console.print(f"Report: {record.report_path}")
    table = Table(title="Critiques")
    table.add_column("Reviewer")
    table.add_column("Subject")
    table.add_column("Status")
    table.add_column("Session")
    table.add_column("Findings")
    table.add_column("Artifact")
    for critique in critiques:
        table.add_row(
            critique.reviewer_provider,
            critique.subject_provider,
            critique.status,
            critique.session_id[:8] if critique.session_id else "-",
            str(len(critique.findings)),
            critique.artifact_path or "-",
        )
    console.print(table)


@app.command("cross-review-artifacts")
def cross_review_artifacts(
    cross_review_id: str,
    config: Path | None = typer.Option(None, "--config", help="Config file path."),
) -> None:
    """List artifacts captured for a cross-review."""
    loaded = load_config(config)
    for path in Store(loaded.global_config.state_dir).list_artifacts(cross_review_id):
        console.print(str(path))


@app.command()
def review(run_id: str) -> None:
    """Run Codex review on the workspace for an existing run."""
    engine = LoopEngine()
    result = engine.run_review(run_id)
    console.print(result.raw_output or json.dumps(result.model_dump(), indent=2))


@app.command()
def attention() -> None:
    """Show runs, sessions, queue items, daemons, duels, and cross-reviews ordered by priority."""
    config = load_config()
    router = AttentionRouter(Store(config.global_config.state_dir))
    table = Table(title="Attention")
    table.add_column("Score")
    table.add_column("Type")
    table.add_column("ID")
    table.add_column("Class")
    table.add_column("Summary")
    table.add_column("Next")
    for card in router.cards():
        next_action = card.recommended_actions[0] if card.recommended_actions else "-"
        table.add_row(str(card.score), card.subject_type, card.run_id[:8], card.classification, card.summary, next_action)
    console.print(table)


@app.command()
def metrics() -> None:
    """Print orchestration metrics as JSON."""
    config = load_config()
    router = AttentionRouter(Store(config.global_config.state_dir))
    console.print_json(json.dumps(router.metrics()))


@queue_app.command("list")
def queue_list(status: str | None = typer.Option(None, "--status"), limit: int = typer.Option(100, "--limit")) -> None:
    """List queued, running, and completed queue items."""
    if status is not None and status not in {"queued", "running", "succeeded", "failed", "cancelled"}:
        raise typer.BadParameter("status must be one of: queued, running, succeeded, failed, cancelled")
    config = load_config()
    items = Store(config.global_config.state_dir).list_queue_items(status=status, limit=limit)  # type: ignore[arg-type]
    table = Table(title="Queue")
    table.add_column("Item")
    table.add_column("Kind")
    table.add_column("Status")
    table.add_column("Priority")
    table.add_column("Result")
    table.add_column("Error")
    for item in items:
        table.add_row(item.id[:8], item.kind, item.status, str(item.priority), item.result_id or "-", item.error or "-")
    console.print(table)


@queue_app.command("cancel")
def queue_cancel(item_id: str) -> None:
    """Cancel a queued or running queue item record."""
    config = load_config()
    Store(config.global_config.state_dir).cancel_queue_item(item_id)
    console.print(f"Cancelled queue item {item_id}")


@queue_app.command("requeue")
def queue_requeue(item_id: str) -> None:
    """Move a failed or running queue item back to queued."""
    config = load_config()
    Store(config.global_config.state_dir).requeue_queue_item(item_id)
    console.print(f"Requeued queue item {item_id}")


@queue_app.command("work")
def queue_work(
    once: bool = typer.Option(False, "--once", help="Process at most one queue item."),
    poll_seconds: float = typer.Option(2.0, "--poll-seconds"),
) -> None:
    """Run a foreground queue worker."""
    worker = QueueWorker()
    if once:
        item = worker.process_once()
        console.print("No queued work." if item is None else f"Processed queue item {item.id} -> {item.status}")
        return
    console.print(f"Starting Khan queue worker {worker.worker_id}")
    worker.run_forever(poll_seconds=poll_seconds)


@daemon_app.command("run")
def daemon_run(
    daemon_id: str | None = typer.Option(None, "--daemon-id"),
    poll_seconds: float = typer.Option(2.0, "--poll-seconds"),
    lease_timeout_seconds: float = typer.Option(900.0, "--lease-timeout-seconds"),
    config: Path | None = typer.Option(None, "--config", help="Config file path for supervised daemon children."),
) -> None:
    """Run Khan's foreground daemon loop."""
    worker = QueueWorker(config, daemon_id=daemon_id, lease_timeout_seconds=lease_timeout_seconds)
    console.print(f"Starting Khan daemon {worker.worker_id}")
    worker.run_forever(poll_seconds=poll_seconds)


@daemon_app.command("start")
def daemon_start(
    poll_seconds: float = typer.Option(2.0, "--poll-seconds"),
    lease_timeout_seconds: float = typer.Option(900.0, "--lease-timeout-seconds"),
    config: Path | None = typer.Option(None, "--config", help="Config file path for the daemon process."),
) -> None:
    """Start Khan's daemon as a detached background process."""
    try:
        daemon = DaemonSupervisor(config).start(
            poll_seconds=poll_seconds,
            lease_timeout_seconds=lease_timeout_seconds,
        )
    except DaemonSupervisorError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"Started Khan daemon {daemon.id} pid={daemon.pid}")


@daemon_app.command("status")
def daemon_status(
    config: Path | None = typer.Option(None, "--config", help="Config file path for the daemon state store."),
) -> None:
    """List recent daemon process records."""
    table = Table(title="Daemons")
    table.add_column("Daemon")
    table.add_column("PID")
    table.add_column("Status")
    table.add_column("Heartbeat")
    table.add_column("Last Item")
    table.add_column("Error")
    for daemon in DaemonSupervisor(config).status():
        table.add_row(
            daemon.id[:8],
            str(daemon.pid),
            daemon.status,
            daemon.heartbeat_at.isoformat(),
            daemon.last_queue_item_id[:8] if daemon.last_queue_item_id else "-",
            daemon.error or "-",
        )
    console.print(table)


@daemon_app.command("stop")
def daemon_stop(
    daemon_id: str | None = typer.Option(None, "--daemon-id"),
    config: Path | None = typer.Option(None, "--config", help="Config file path for the daemon state store."),
) -> None:
    """Stop an active Khan daemon."""
    try:
        daemon = DaemonSupervisor(config).stop(daemon_id)
    except DaemonSupervisorError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"Stop requested for daemon {daemon.id} status={daemon.status}")


@app.command()
def watch(run_id: str, poll_seconds: float = 1.5) -> None:
    """Follow run status and events until the run reaches a terminal or human-input state."""
    config = load_config()
    store = Store(config.global_config.state_dir)
    seen = 0
    try:
        while True:
            run = store.get_run(run_id)
            events = store.list_events(run_id, limit=200)
            console.clear()
            console.print(f"[bold]Run[/bold] {run.id}  [bold]Status[/bold] {run.status}  [bold]Iteration[/bold] {run.iteration}")
            for event in events[seen:]:
                console.print(f"{event.ts.isoformat()} [{event.phase}] {event.message}")
            seen = len(events)
            if run.status in {"succeeded", "failed", "needs_human", "cancelled"}:
                break
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        console.print("Stopped watching.")


@app.command()
def tui() -> None:
    """Open the Textual operator console."""
    from .tui import KhanApp

    KhanApp().run()


@app.command()
def list_tasks() -> None:
    """List stored tasks."""
    config = load_config()
    store = Store(config.global_config.state_dir)
    table = Table(title="Tasks")
    table.add_column("Task ID")
    table.add_column("Project")
    table.add_column("Title")
    table.add_column("Created")
    for task in store.list_tasks():
        table.add_row(task.id, task.project, task.title, task.created_at.isoformat())
    console.print(table)


def main() -> None:
    app(prog_name="khan")

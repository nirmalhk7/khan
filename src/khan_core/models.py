from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


AgentProvider = str
AgentSessionStatus = Literal[
    "queued",
    "running",
    "stopping",
    "succeeded",
    "failed",
    "cancelled",
]

QueueItemKind = Literal["task", "session", "pipeline"]
QueueItemStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
DaemonStatus = Literal["running", "stopping", "stopped", "failed"]
DuelStatus = Literal["queued", "running", "awaiting_decision", "adopted", "rejected", "failed", "cancelled"]
DuelParticipantStatus = Literal["queued", "running", "succeeded", "adopted", "rejected", "failed", "cancelled"]
AdoptionTargetType = Literal["duel", "session", "run", "pipeline"]
AdoptionStatus = Literal["adopted", "rejected", "failed"]
CrossReviewStatus = Literal["queued", "running", "awaiting_decision", "failed"]
CrossReviewCritiqueStatus = Literal["queued", "running", "succeeded", "failed"]
PipelineStatus = Literal["queued", "planning", "building", "reviewing", "awaiting_decision", "adopted", "rejected", "failed", "cancelled"]
PipelinePhaseStatus = Literal["queued", "running", "succeeded", "failed", "skipped"]

RunStatus = Literal[
    "queued",
    "planning",
    "preflight",
    "checkpointing",
    "running",
    "paused",
    "awaiting_decision",
    "stopping",
    "validating",
    "reviewing",
    "retryable_failure",
    "needs_human",
    "succeeded",
    "failed",
    "cancelled",
]


class LoopProfile(BaseModel):
    max_iterations: int = 4
    max_review_rounds: int = 2
    max_validation_retries: int = 2
    max_runtime_minutes: int = 45
    idle_timeout_minutes: int = 10
    checkpoint_before_run: bool = True
    checkpoint_on_success: bool = False
    auto_review: bool = True
    require_human_approval_after: int = 2
    stop_on_repeated_failure: bool = True
    stop_on_empty_diff: bool = True


class GlobalConfig(BaseModel):
    codex_bin: str = "codex"
    codex_model: str = "gpt-5.4-mini"
    codex_reasoning_effort: str = "high"
    cursor_agent_bin: str = "cursor-agent"
    state_dir: Path = Field(default_factory=lambda: Path.home() / ".khan")
    default_profile: str = "default"
    max_concurrent_runs: int = 3


class NotificationConfig(BaseModel):
    input_needed: bool = True
    say_bin: str = "say"
    phrase: str = "Khan needs your input."


class AdoptionConfig(BaseModel):
    retention_days: int = 7


class DaemonConfig(BaseModel):
    stale_heartbeat_seconds: int = 900
    restart_on_crash: bool = False


class ProjectConfig(BaseModel):
    name: str
    path: Path
    default_branch: str = "main"
    codex_profile: str | None = None
    sandbox: str = "workspace-write"
    approval_policy: str = "on-request"
    workspace_mode: Literal["auto", "worktree", "in_place"] = "auto"
    validate_commands: list[str] = Field(default_factory=list)
    build_commands: list[str] = Field(default_factory=list)
    review_prompt: str = (
        "Review the current changes for correctness, regressions, and missing tests. "
        "The first line must be exactly one of: VERDICT: PASS, VERDICT: FIX, VERDICT: ESCALATE."
    )
    protected_paths: list[str] = Field(default_factory=list)
    skills_hint: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class ConfigFile(BaseModel):
    global_config: GlobalConfig = Field(default_factory=GlobalConfig, alias="global")
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    adoption: AdoptionConfig = Field(default_factory=AdoptionConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    profiles: dict[str, LoopProfile] = Field(default_factory=lambda: {"default": LoopProfile()})
    projects: dict[str, ProjectConfig] = Field(default_factory=dict)
    prompts: dict[str, str] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class TaskRecord(BaseModel):
    id: str
    project: str
    title: str
    prompt: str
    success_criteria: str
    profile: str | None = None
    created_at: datetime


class RunRecord(BaseModel):
    id: str
    task_id: str
    project: str
    status: RunStatus
    iteration: int = 1
    workspace: str
    created_at: datetime
    updated_at: datetime
    summary: str = ""
    session_id: str | None = None
    process_id: int | None = None
    heartbeat_at: datetime | None = None
    failure_fingerprint: str | None = None
    repeated_failures: int = 0


class EventRecord(BaseModel):
    run_id: str
    ts: datetime
    phase: str
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentSessionRecord(BaseModel):
    id: str
    provider: AgentProvider
    project: str
    status: AgentSessionStatus
    workspace: str
    prompt: str
    summary: str = ""
    parent_session_id: str | None = None
    external_id: str | None = None
    process_id: int | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AgentSessionEvent(BaseModel):
    session_id: str
    ts: datetime
    stream: Literal["stdout", "stderr", "system"]
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)


class QueueItemRecord(BaseModel):
    id: str
    kind: QueueItemKind
    status: QueueItemStatus
    priority: int = 100
    payload: dict[str, Any] = Field(default_factory=dict)
    result_id: str | None = None
    error: str = ""
    attempts: int = 0
    lease_owner: str | None = None
    leased_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class DaemonRecord(BaseModel):
    id: str
    pid: int
    status: DaemonStatus
    command: list[str] = Field(default_factory=list)
    poll_seconds: float = 2.0
    lease_timeout_seconds: float = 900.0
    started_at: datetime
    heartbeat_at: datetime
    stopped_at: datetime | None = None
    last_queue_item_id: str | None = None
    error: str = ""


class DuelRecord(BaseModel):
    id: str
    project: str
    prompt: str
    providers: list[AgentProvider] = Field(default_factory=list)
    status: DuelStatus
    summary: str = ""
    report_path: str = ""
    created_at: datetime
    updated_at: datetime


class DuelParticipantRecord(BaseModel):
    duel_id: str
    provider: AgentProvider
    session_id: str | None = None
    status: DuelParticipantStatus
    workspace: str = ""
    changed_files: list[str] = Field(default_factory=list)
    diff_stat: str = ""
    validation_ok: bool | None = None
    validation_summary: str = ""
    runtime_seconds: float = 0.0
    summary: str = ""
    open_risks: list[str] = Field(default_factory=list)
    artifact_path: str = ""
    created_at: datetime
    updated_at: datetime


class AdoptionRecord(BaseModel):
    id: str
    target_type: AdoptionTargetType
    target_id: str
    provider: AgentProvider | None = None
    session_id: str | None = None
    project: str
    source_workspace: str
    destination_workspace: str
    status: AdoptionStatus
    changed_files: list[str] = Field(default_factory=list)
    summary: str = ""
    error: str = ""
    created_at: datetime


class CrossReviewRecord(BaseModel):
    id: str
    duel_id: str
    status: CrossReviewStatus
    summary: str = ""
    report_path: str = ""
    created_at: datetime
    updated_at: datetime


class CrossReviewCritiqueRecord(BaseModel):
    cross_review_id: str
    duel_id: str
    reviewer_provider: AgentProvider
    subject_provider: AgentProvider
    session_id: str | None = None
    status: CrossReviewCritiqueStatus
    verdict: Literal["PASS", "FIX", "ESCALATE"] = "ESCALATE"
    strongest_implementation: AgentProvider | None = None
    reviewer_disagreement: bool = False
    required_human_inspection: bool = False
    summary: str = ""
    findings: list[str] = Field(default_factory=list)
    artifact_path: str = ""
    created_at: datetime
    updated_at: datetime


class PipelineRecord(BaseModel):
    id: str
    task_id: str
    project: str
    prompt: str
    status: PipelineStatus
    planner_provider: AgentProvider = "codex"
    builder_providers: list[AgentProvider] = Field(default_factory=list)
    recommended_provider: AgentProvider | None = None
    decision_summary: str = ""
    report_path: str = ""
    created_at: datetime
    updated_at: datetime


class PipelinePhaseRecord(BaseModel):
    pipeline_id: str
    phase: Literal["plan", "build", "review", "decide"]
    provider: AgentProvider | None = None
    status: PipelinePhaseStatus = "queued"
    session_id: str | None = None
    duel_id: str | None = None
    cross_review_id: str | None = None
    artifact_path: str = ""
    summary: str = ""
    created_at: datetime
    updated_at: datetime


class WorkerResult(BaseModel):
    status: Literal["done", "blocked", "needs_review", "needs_human"]
    summary: str
    changed_files: list[str] = Field(default_factory=list)
    tests_run: list[str] = Field(default_factory=list)
    open_risks: list[str] = Field(default_factory=list)
    next_action: str = ""


class ValidationResult(BaseModel):
    ok: bool
    command_results: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""


class ReviewResult(BaseModel):
    verdict: Literal["PASS", "FIX", "ESCALATE"]
    findings: list[str] = Field(default_factory=list)
    raw_output: str = ""


AskMode = Literal["pipeline", "single", "queue"]


class RunProcess(BaseModel):
    run_id: str
    pid: int
    command: list[str]
    started_at: datetime
    heartbeat_at: datetime
    ended_at: datetime | None = None
    returncode: int | None = None


class RunCommand(BaseModel):
    id: int
    run_id: str
    command: Literal["pause", "resume", "cancel", "steer", "retry", "approve"]
    payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "applied", "rejected"] = "pending"
    created_at: datetime
    applied_at: datetime | None = None


class TaskCapsule(BaseModel):
    objective: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    expected_files: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    protected_paths: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    blast_radius: Literal["small", "medium", "large"] = "small"
    dependencies: list[str] = Field(default_factory=list)
    conflict_domains: list[str] = Field(default_factory=list)


class DecisionCard(BaseModel):
    run_id: str
    subject_type: Literal["run", "session", "queue", "daemon", "duel", "cross_review", "pipeline"] = "run"
    classification: Literal["healthy", "watch", "decision_required", "stopped"]
    score: int
    summary: str
    evidence: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    recommended_provider: AgentProvider | None = None
    confidence: Literal["low", "medium", "high"] | None = None
    primary_action: str = ""
    secondary_actions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class FailureFingerprint(BaseModel):
    value: str
    count: int = 1
    summary: str = ""


class VerificationRecipe(BaseModel):
    commands: list[str] = Field(default_factory=list)
    successful_runs: int = 0

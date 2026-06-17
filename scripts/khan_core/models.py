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

QueueItemKind = Literal["task", "session"]
QueueItemStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
DaemonStatus = Literal["running", "stopping", "stopped", "failed"]

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
    cursor_agent_bin: str = "cursor-agent"
    state_dir: Path = Field(default_factory=lambda: Path.home() / ".khan")
    default_profile: str = "default"
    max_concurrent_runs: int = 3


class NotificationConfig(BaseModel):
    input_needed: bool = True
    say_bin: str = "say"
    phrase: str = "Khan needs your input."


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
    subject_type: Literal["run", "session", "queue", "daemon"] = "run"
    classification: Literal["healthy", "watch", "decision_required", "stopped"]
    score: int
    summary: str
    evidence: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)


class FailureFingerprint(BaseModel):
    value: str
    count: int = 1
    summary: str = ""


class VerificationRecipe(BaseModel):
    commands: list[str] = Field(default_factory=list)
    successful_runs: int = 0

from __future__ import annotations

import json
import hashlib
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .codex_cli import CodexCLI, CodexCancelled, CodexInvocationError
from .config import load_config
from .models import EventRecord, ProjectConfig, ReviewResult, TaskCapsule, TaskRecord, ValidationResult, WorkerResult
from .notifier import InputNotifier
from .prompt_builder import build_worker_prompt, write_schema
from .reviewer import Reviewer
from .store import Store
from .validator import Validator
from .worktree import WorktreeManager


class LoopEngine:
    def __init__(self, config_path: Path | None = None) -> None:
        self.config = load_config(config_path)
        self.store = Store(self.config.global_config.state_dir)
        self.codex = CodexCLI(self.config.global_config.codex_bin)
        self.validator = Validator()
        self.reviewer = Reviewer(self.codex)
        self.worktrees = WorktreeManager(self.config.global_config.state_dir)
        self.notifier = InputNotifier(self.config)

    def run_task(self, task_id: str) -> str:
        task = self.store.get_task(task_id)
        project = self._project(task.project)
        capsule = self.store.get_task_capsule(task.id)
        with self.store.run_lock("__scheduler__"):
            if len(self.store.list_runs(active_only=True)) >= self.config.global_config.max_concurrent_runs:
                raise RuntimeError("Maximum concurrent run limit reached.")
            conflict_domains = sorted(self.store.effective_conflict_domains(capsule))
            conflicting_run = self.store.find_conflicting_active_run(project.name, task.id, conflict_domains)
            if conflicting_run:
                domains = ", ".join(conflict_domains)
                raise RuntimeError(
                    f"Task conflicts with active run {conflicting_run.id} in domains: {domains}"
                )
            active_runs = self.store.active_runs_for_project(project.name)
            force_worktree = project.workspace_mode == "worktree" or bool(active_runs)
            workspace_key = str(uuid.uuid4())
            workspace, is_worktree = self.worktrees.choose_workspace(project, workspace_key, force_worktree)
            run = self.store.create_run(task.id, project.name, str(workspace))

        try:
            with self.store.run_lock(run.id):
                return self._run_loop(run.id, task, project, workspace, capsule)
        finally:
            final_status = self.store.get_run(run.id).status
            if is_worktree and final_status not in {"succeeded", "needs_human", "awaiting_decision"}:
                self.worktrees.cleanup(project, workspace, is_worktree=True)

    def _run_loop(
        self,
        run_id: str,
        task: TaskRecord,
        project: ProjectConfig,
        workspace: Path,
        capsule: TaskCapsule,
    ) -> str:
        profile = self._profile_name(task, project)
        prior_failures: list[str] = []
        review_findings: list[str] = []
        review_rounds = 0
        last_fingerprint: str | None = None
        repeated_failures = 0
        try:
            for iteration in range(1, profile.max_iterations + 1):
                self.store.update_run(run_id, "preflight", f"Preparing iteration {iteration}", iteration=iteration)
                self._event(run_id, "preflight", f"Starting iteration {iteration}", {"workspace": str(workspace)})
                if profile.checkpoint_before_run:
                    self._snapshot_checkpoint(run_id, workspace)

                self.store.write_artifact(run_id, "task-capsule.json", capsule.model_dump_json(indent=2))
                prompt = build_worker_prompt(task, project, iteration, prior_failures, review_findings, capsule)
                run_dir = self.store.run_dir(run_id)
                prompt_path = self.store.write_artifact(run_id, f"prompt-{iteration}.md", prompt)
                schema_path = write_schema(self.config.global_config.state_dir / "schemas" / f"{run_id}.json")
                output_path = run_dir / "last-message.json"

                self.store.update_run(run_id, "running", f"Worker iteration {iteration}", iteration=iteration)
                self._event(run_id, "running", "Invoking Codex worker", {"prompt_path": str(prompt_path)})
                try:
                    worker_result, _ = self.codex.exec_task(
                        workspace, prompt, schema_path, output_path, project, run_id=run_id,
                        on_process=self.store.start_process,
                        on_finish=lambda pid, rc: self.store.finish_process(run_id, pid, rc),
                        on_heartbeat=lambda pid: self.store.heartbeat(run_id, pid),
                        on_event=lambda event: self._codex_event(run_id, event),
                        commands=lambda: [(command.id, command.command) for command in self.store.pending_commands(run_id)],
                        command_applied=self.store.apply_command,
                        timeout_seconds=profile.max_runtime_minutes * 60,
                        idle_timeout_seconds=profile.idle_timeout_minutes * 60,
                    )
                except CodexCancelled as exc:
                    self.store.update_run(run_id, "cancelled", str(exc), iteration=iteration)
                    self._event(run_id, "cancelled", str(exc), {})
                    return run_id
                except CodexInvocationError as exc:
                    prior_failures.append(str(exc))
                    self._event(run_id, "running", "Codex worker failed", {"error": str(exc)})
                    last_fingerprint, repeated_failures = self._track_failure(
                        run_id, str(exc), last_fingerprint, repeated_failures)
                    if profile.stop_on_repeated_failure and repeated_failures >= 2:
                        self._needs_human(
                            run_id,
                            "Repeated worker failure.",
                            iteration=iteration,
                            payload={"failure_fingerprint": last_fingerprint, "repeated_failures": repeated_failures},
                            failure_fingerprint=last_fingerprint,
                            repeated_failures=repeated_failures,
                        )
                        return run_id
                    if iteration >= profile.max_iterations:
                        self.store.update_run(run_id, "failed", str(exc), iteration=iteration)
                        return run_id
                    continue

                changed_files = self._changed_files(workspace)
                worker_result.changed_files = changed_files
                self._event(run_id, "running", "Worker completed", worker_result.model_dump())

                if self._protected_paths_touched(project, capsule, changed_files):
                    summary = "Protected path changed; stopping for human review."
                    self._needs_human(run_id, summary, iteration=iteration, payload={"changed_files": changed_files})
                    return run_id
                outside_allowed = self._outside_allowed_paths(capsule, changed_files)
                if outside_allowed:
                    summary = "Worker changed files outside the task capsule allowed paths."
                    self._needs_human(
                        run_id,
                        summary,
                        iteration=iteration,
                        payload={"changed_files": changed_files, "outside_allowed_paths": outside_allowed},
                    )
                    return run_id
                if profile.stop_on_empty_diff and not changed_files:
                    summary = "Worker completed without producing a diff."
                    self._needs_human(run_id, summary, iteration=iteration)
                    return run_id

                if worker_result.status in {"blocked", "needs_human"}:
                    self._needs_human(run_id, worker_result.summary, iteration=iteration,
                                      payload=worker_result.model_dump())
                    return run_id

                self.store.update_run(run_id, "validating", "Running validation", iteration=iteration)
                validation = self.validator.run(workspace, project.validate_commands,
                                                timeout_seconds=profile.max_runtime_minutes * 60)
                self.store.write_artifact(run_id, f"validation-{iteration}.json", validation.model_dump_json(indent=2))
                self._event(run_id, "validating", validation.summary, validation.model_dump())
                if not validation.ok:
                    prior_failures = self._summarize_validation(validation)
                    review_findings = []
                    fingerprint_text = "\n".join(prior_failures)
                    last_fingerprint, repeated_failures = self._track_failure(
                        run_id, fingerprint_text, last_fingerprint, repeated_failures)
                    self.store.update_run(run_id, "retryable_failure", validation.summary, iteration=iteration,
                                          failure_fingerprint=last_fingerprint,
                                          repeated_failures=repeated_failures)
                    if (profile.stop_on_repeated_failure and repeated_failures >= 2) or \
                            iteration >= profile.max_validation_retries + 1:
                        self._needs_human(
                            run_id,
                            validation.summary,
                            iteration=iteration,
                            payload=validation.model_dump(),
                            failure_fingerprint=last_fingerprint,
                            repeated_failures=repeated_failures,
                        )
                        return run_id
                    continue

                if profile.auto_review:
                    review_rounds += 1
                    if review_rounds > profile.max_review_rounds:
                        self._needs_human(run_id, "Review round limit reached.", iteration=iteration)
                        return run_id
                    self.store.update_run(run_id, "reviewing", "Running review", iteration=iteration)
                    review = self.reviewer.run(workspace, project)
                    self.store.write_artifact(run_id, f"review-{review_rounds}.md", review.raw_output)
                    self._event(run_id, "reviewing", f"Reviewer verdict: {review.verdict}", review.model_dump())
                    if review.verdict == "ESCALATE":
                        self._needs_human(run_id, "Reviewer escalated the run.", iteration=iteration,
                                          payload=review.model_dump())
                        return run_id
                    if review.verdict == "FIX":
                        review_findings = review.findings or ["Reviewer requested fixes."]
                        prior_failures = []
                        self.store.update_run(run_id, "retryable_failure", "Reviewer requested fixes.", iteration=iteration)
                        continue

                if profile.checkpoint_on_success:
                    self._snapshot_success(run_id, workspace, changed_files)
                self.store.write_artifact(run_id, "summary.md", worker_result.summary)
                self.store.update_run(run_id, "succeeded", worker_result.summary, iteration=iteration)
                self._event(run_id, "succeeded", worker_result.summary, worker_result.model_dump())
                return run_id

            self.store.update_run(run_id, "failed", "Loop exhausted without success.")
            return run_id
        except Exception as exc:
            self.store.update_run(run_id, "failed", str(exc))
            self._event(run_id, "failed", str(exc), {"type": type(exc).__name__})
            raise

    def retry_run(self, run_id: str) -> str:
        run = self.store.get_run(run_id)
        task = self.store.get_task(run.task_id)
        return self.run_task(task.id)

    def run_review(self, run_id: str) -> ReviewResult:
        run = self.store.get_run(run_id)
        task = self.store.get_task(run.task_id)
        project = self._project(task.project)
        return self.reviewer.run(Path(run.workspace), project)

    def _profile_name(self, task: TaskRecord, project: ProjectConfig):
        profile_name = task.profile or project.codex_profile or self.config.global_config.default_profile
        return self.config.profiles[profile_name]

    def _project(self, project_name: str) -> ProjectConfig:
        if project_name not in self.config.projects:
            raise KeyError(f"Project not found in config: {project_name}")
        return self.config.projects[project_name]

    def _event(self, run_id: str, phase: str, message: str, payload: dict) -> None:
        self.store.append_event(
            EventRecord(
                run_id=run_id,
                ts=datetime.now(UTC),
                phase=phase,
                message=message,
                payload=payload,
            )
        )

    def _needs_human(
        self,
        run_id: str,
        summary: str,
        *,
        iteration: int | None = None,
        payload: dict | None = None,
        failure_fingerprint: str | None = None,
        repeated_failures: int | None = None,
    ) -> None:
        self.store.update_run(
            run_id,
            "needs_human",
            summary,
            iteration=iteration,
            failure_fingerprint=failure_fingerprint,
            repeated_failures=repeated_failures,
        )
        self._event(run_id, "needs_human", summary, payload or {})
        notified = self.notifier.notify_input_needed(run_id, summary, subject_type="run")
        self._event(run_id, "notify", "Input-needed notification sent" if notified else "Input-needed notification skipped",
                    {"channel": "say", "sent": notified})

    def _codex_event(self, run_id: str, event: dict) -> None:
        self.store.append_jsonl(run_id, "codex-output.jsonl", [event])
        session_id = event.get("thread_id") or event.get("session_id")
        if isinstance(session_id, str) and session_id:
            self.store.set_session_id(run_id, session_id)

    def _snapshot_checkpoint(self, run_id: str, workspace: Path) -> None:
        try:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=workspace,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            status = subprocess.run(
                ["git", "status", "--short"],
                cwd=workspace,
                text=True,
                capture_output=True,
                check=True,
            ).stdout
        except subprocess.CalledProcessError:
            return
        checkpoint = {"head": head, "status": status}
        self.store.write_artifact(run_id, "checkpoint.json", json.dumps(checkpoint, indent=2))

    def _snapshot_success(self, run_id: str, workspace: Path, changed_files: list[str]) -> None:
        try:
            diff_stat = subprocess.run(
                ["git", "diff", "--stat", "HEAD"],
                cwd=workspace,
                text=True,
                capture_output=True,
                check=True,
            ).stdout
        except subprocess.CalledProcessError:
            diff_stat = ""
        self.store.write_artifact(
            run_id,
            "success-checkpoint.json",
            json.dumps({"changed_files": changed_files, "diff_stat": diff_stat}, indent=2),
        )

    def _changed_files(self, workspace: Path) -> list[str]:
        tracked = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"], cwd=workspace, text=True, capture_output=True, check=True
        ).stdout.splitlines()
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"], cwd=workspace, text=True,
            capture_output=True, check=True
        ).stdout.splitlines()
        return sorted(set(tracked + untracked))

    def _track_failure(self, run_id: str, text: str, previous: str | None, count: int) -> tuple[str, int]:
        normalized = " ".join(text.lower().split())
        fingerprint = hashlib.sha256(normalized.encode()).hexdigest()[:16]
        next_count = count + 1 if fingerprint == previous else 1
        self._event(run_id, "failure", "Recorded failure fingerprint",
                    {"fingerprint": fingerprint, "repeated": next_count})
        return fingerprint, next_count

    def _summarize_validation(self, validation: ValidationResult) -> list[str]:
        failures: list[str] = []
        for result in validation.command_results:
            if result["returncode"] != 0:
                stderr = (result.get("stderr") or "").strip()
                stdout = (result.get("stdout") or "").strip()
                snippet = stderr or stdout or "no output"
                failures.append(f"{result['command']} failed: {snippet[:400]}")
        return failures or [validation.summary]

    def _protected_paths_touched(self, project: ProjectConfig, capsule: TaskCapsule, changed_files: list[str]) -> bool:
        protected_paths = [*project.protected_paths, *capsule.protected_paths]
        if not protected_paths:
            return False
        for changed in changed_files:
            for protected in protected_paths:
                if changed == protected or changed.startswith(f"{protected}/"):
                    return True
        return False

    def _outside_allowed_paths(self, capsule: TaskCapsule, changed_files: list[str]) -> list[str]:
        if not capsule.allowed_paths:
            return []
        outside = []
        for changed in changed_files:
            allowed = any(changed == path or changed.startswith(f"{path}/") for path in capsule.allowed_paths)
            if not allowed:
                outside.append(changed)
        return outside

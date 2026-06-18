from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import load_config
from .models import AdoptionRecord, AgentProvider, ProjectConfig
from .store import Store
from .worktree import WorktreeManager


class AdoptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChangeEntry:
    path: str
    status: str
    old_path: str | None = None


@dataclass(frozen=True)
class AdoptionTarget:
    target_type: str
    target_id: str
    project: ProjectConfig
    source_workspace: Path
    provider: AgentProvider | None = None
    session_id: str | None = None


class AdoptionManager:
    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        self.store = Store(self.config.global_config.state_dir)
        self.worktrees = WorktreeManager(self.config.global_config.state_dir)

    def adopt(self, target_id: str, *, provider: AgentProvider | None = None, force: bool = False, cleanup: bool = False) -> AdoptionRecord:
        target = self._resolve_target(target_id, provider)
        destination = target.project.path
        changes = self._changed_entries(target.source_workspace)
        changed_files = self._changed_files(changes)
        try:
            if target.source_workspace.resolve() == destination.resolve():
                raise AdoptionError("Cannot adopt from the destination workspace itself.")
            if not changes:
                raise AdoptionError("No changes to adopt.")
            dirty = self._git_status(destination)
            if dirty and not force:
                raise AdoptionError("Destination worktree is dirty; commit/stash changes or retry with --force.")
            self._apply_changes(target.source_workspace, destination, changes)
            if cleanup:
                self._cleanup_source(target)
            summary = self._summary("Adopted", changed_files, target.project)
            record = self.store.create_adoption_decision(
                target_type=target.target_type,  # type: ignore[arg-type]
                target_id=target.target_id,
                provider=target.provider,
                session_id=target.session_id,
                project=target.project.name,
                source_workspace=str(target.source_workspace),
                destination_workspace=str(destination),
                status="adopted",
                changed_files=changed_files,
                summary=summary,
            )
            self._mark_target(target, "adopted", summary)
            return record
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.store.create_adoption_decision(
                target_type=target.target_type,  # type: ignore[arg-type]
                target_id=target.target_id,
                provider=target.provider,
                session_id=target.session_id,
                project=target.project.name,
                source_workspace=str(target.source_workspace),
                destination_workspace=str(destination),
                status="failed",
                changed_files=changed_files,
                summary="Adoption failed.",
                error=error,
            )
            if isinstance(exc, AdoptionError):
                raise
            raise AdoptionError(error) from exc

    def reject(self, target_id: str, *, provider: AgentProvider | None = None, cleanup: bool = True) -> AdoptionRecord:
        target = self._resolve_target(target_id, provider)
        changes = self._changed_entries(target.source_workspace)
        changed_files = self._changed_files(changes)
        if cleanup:
            self._cleanup_source(target)
        summary = self._summary("Rejected", changed_files, target.project)
        record = self.store.create_adoption_decision(
            target_type=target.target_type,  # type: ignore[arg-type]
            target_id=target.target_id,
            provider=target.provider,
            session_id=target.session_id,
            project=target.project.name,
            source_workspace=str(target.source_workspace),
            destination_workspace=str(target.project.path),
            status="rejected",
            changed_files=changed_files,
            summary=summary,
        )
        self._mark_target(target, "rejected", summary)
        return record

    def _resolve_target(self, target_id: str, provider: AgentProvider | None) -> AdoptionTarget:
        if provider:
            try:
                duel = self.store.get_duel(target_id)
                participant = self.store.get_duel_participant(duel.id, provider)
                project = self._project(duel.project)
                return AdoptionTarget(
                    target_type="duel",
                    target_id=duel.id,
                    provider=participant.provider,
                    session_id=participant.session_id,
                    project=project,
                    source_workspace=Path(participant.workspace),
                )
            except KeyError as exc:
                raise AdoptionError(f"Duel participant not found: {target_id}/{provider}") from exc

        try:
            duel = self.store.get_duel(target_id)
            participants = self.store.list_duel_participants(duel.id)
            if len(participants) != 1:
                raise AdoptionError("Duel has multiple participants; pass --provider.")
            participant = participants[0]
            return AdoptionTarget(
                target_type="duel",
                target_id=duel.id,
                provider=participant.provider,
                session_id=participant.session_id,
                project=self._project(duel.project),
                source_workspace=Path(participant.workspace),
            )
        except KeyError:
            pass

        try:
            session = self.store.get_agent_session(target_id)
            return AdoptionTarget(
                target_type="session",
                target_id=session.id,
                provider=session.provider,
                session_id=session.id,
                project=self._project(session.project),
                source_workspace=Path(session.workspace),
            )
        except KeyError:
            pass

        try:
            run = self.store.get_run(target_id)
            return AdoptionTarget(
                target_type="run",
                target_id=run.id,
                project=self._project(run.project),
                source_workspace=Path(run.workspace),
            )
        except KeyError as exc:
            raise AdoptionError(f"No adoptable run, session, or duel found for: {target_id}") from exc

    def _project(self, project_name: str) -> ProjectConfig:
        try:
            return self.config.projects[project_name]
        except KeyError as exc:
            raise AdoptionError(f"Project not found in config: {project_name}") from exc

    def _mark_target(self, target: AdoptionTarget, status: str, summary: str) -> None:
        if target.target_type != "duel" or not target.provider:
            return
        participant = self.store.get_duel_participant(target.target_id, target.provider)
        self.store.upsert_duel_participant(
            target.target_id,
            target.provider,
            status,  # type: ignore[arg-type]
            session_id=participant.session_id,
            workspace=participant.workspace,
            changed_files=participant.changed_files,
            diff_stat=participant.diff_stat,
            validation_ok=participant.validation_ok,
            validation_summary=participant.validation_summary,
            runtime_seconds=participant.runtime_seconds,
            summary=participant.summary,
            open_risks=participant.open_risks,
            artifact_path=participant.artifact_path,
        )
        if status == "adopted":
            self.store.update_duel(target.target_id, "adopted", summary)
            return
        participants = self.store.list_duel_participants(target.target_id)
        if any(p.status == "adopted" for p in participants):
            duel_status = "adopted"
        elif participants and all(p.status == "rejected" for p in participants):
            duel_status = "rejected"
        else:
            duel_status = "awaiting_decision"
        self.store.update_duel(target.target_id, duel_status, summary)

    def _cleanup_source(self, target: AdoptionTarget) -> None:
        if not target.source_workspace.exists() or target.source_workspace.resolve() == target.project.path.resolve():
            return
        self.worktrees.cleanup(target.project, target.source_workspace, is_worktree=True)

    def _apply_changes(self, source: Path, destination: Path, changes: list[ChangeEntry]) -> None:
        for change in changes:
            self._ensure_safe_relative_path(change.path)
            if change.old_path:
                self._ensure_safe_relative_path(change.old_path)
                self._delete_path(destination / change.old_path)
            source_path = source / change.path
            destination_path = destination / change.path
            if source_path.exists():
                self._copy_path(source_path, destination_path)
            elif "D" in change.status:
                self._delete_path(destination_path)

    def _copy_path(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            if destination.exists() and not destination.is_dir():
                destination.unlink()
            shutil.copytree(source, destination, dirs_exist_ok=True)
            return
        shutil.copy2(source, destination)

    def _delete_path(self, path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()

    def _changed_entries(self, workspace: Path) -> list[ChangeEntry]:
        status = self._git_status(workspace)
        entries: list[ChangeEntry] = []
        for line in status.splitlines():
            if not line:
                continue
            code = line[:2]
            path = line[3:]
            old_path = None
            if " -> " in path:
                old_path, path = path.split(" -> ", 1)
            entries.append(ChangeEntry(path=path, status=code, old_path=old_path))
        return entries

    def _changed_files(self, changes: list[ChangeEntry]) -> list[str]:
        files: list[str] = []
        for change in changes:
            if change.old_path:
                files.append(change.old_path)
            files.append(change.path)
        return files

    def _git_status(self, workspace: Path) -> str:
        if not workspace.exists():
            raise AdoptionError(f"Workspace does not exist: {workspace}")
        process = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=workspace,
            text=True,
            capture_output=True,
            check=False,
        )
        if process.returncode != 0:
            raise AdoptionError(process.stderr.strip() or f"Not a git workspace: {workspace}")
        return process.stdout.strip()

    def _summary(self, action: str, changed_files: list[str], project: ProjectConfig) -> str:
        protected = [
            path for path in changed_files
            if any(path == protected_path or path.startswith(f"{protected_path.rstrip('/')}/") for protected_path in project.protected_paths)
        ]
        base = f"{action} {len(changed_files)} changed file(s)."
        if protected:
            return f"{base} Warning: protected paths changed: {', '.join(protected)}."
        return base

    @staticmethod
    def _ensure_safe_relative_path(path: str) -> None:
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts or ".git" in candidate.parts:
            raise AdoptionError(f"Unsafe path in candidate changes: {path}")

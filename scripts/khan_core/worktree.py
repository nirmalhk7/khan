from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .models import ProjectConfig


def git_output(repo: Path, *args: str) -> str:
    process = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True)
    return process.stdout.strip()


class WorktreeManager:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir

    def choose_workspace(self, project: ProjectConfig, run_id: str, force_worktree: bool) -> tuple[Path, bool]:
        if project.workspace_mode == "in_place":
            return project.path, False
        if project.workspace_mode in {"auto", "worktree"} or force_worktree:
            return self.create_worktree(project, run_id), True
        raise ValueError(f"Unsupported workspace mode: {project.workspace_mode}")

    def create_worktree(self, project: ProjectConfig, run_id: str) -> Path:
        root = self.state_dir / "worktrees" / project.name
        root.mkdir(parents=True, exist_ok=True)
        branch = f"khan/{run_id}"
        workspace = root / run_id
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(workspace), project.default_branch],
            cwd=project.path,
            text=True,
            capture_output=True,
            check=True,
        )
        return workspace

    def cleanup(self, project: ProjectConfig, workspace: Path, is_worktree: bool) -> None:
        if not is_worktree:
            return
        subprocess.run(["git", "worktree", "remove", "--force", str(workspace)], cwd=project.path, check=False)
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)
        branch = f"khan/{workspace.name}"
        subprocess.run(["git", "branch", "-D", branch], cwd=project.path, check=False, capture_output=True)

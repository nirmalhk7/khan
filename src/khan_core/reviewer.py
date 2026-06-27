from __future__ import annotations

from pathlib import Path

from .codex_cli import CodexCLI
from .models import ProjectConfig, ReviewResult


class Reviewer:
    def __init__(self, codex: CodexCLI) -> None:
        self.codex = codex

    def run(self, workspace: Path, project: ProjectConfig) -> ReviewResult:
        return self.codex.review_changes(workspace, project.review_prompt, uncommitted=True)

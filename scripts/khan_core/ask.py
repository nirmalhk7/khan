from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from .config import discover_project, load_config, save_config
from .loop_engine import LoopEngine
from .models import ProjectConfig, QueueItemRecord, RunRecord, TaskCapsule, TaskRecord
from .store import Store


class AskError(RuntimeError):
    pass


class AskRunner:
    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        self.store = Store(self.config.global_config.state_dir)

    def ask(
        self,
        target: str,
        prompt: str,
        *,
        title: str | None = None,
        success: str | None = None,
        profile: str | None = None,
        accept: list[str] | None = None,
        verify: list[str] | None = None,
        enqueue: bool = False,
        priority: int = 100,
    ) -> tuple[TaskRecord, RunRecord | None, QueueItemRecord | None]:
        project = self._resolve_project(target)
        success_criteria = success or "The requested task is implemented and verified."
        verification = verify if verify is not None else project.validate_commands
        capsule = TaskCapsule(
            objective=prompt,
            acceptance_criteria=accept or [success_criteria],
            verification=verification,
            conflict_domains=[project.name],
        )
        task = self.store.create_task(
            project.name,
            title or self._title_from_prompt(prompt),
            prompt,
            success_criteria,
            profile,
            capsule,
        )
        if enqueue:
            item = self.store.enqueue_task(task.id, priority=priority)
            return task, None, item
        engine = LoopEngine(self.config_path)
        run_id = engine.run_task(task.id)
        return task, engine.store.get_run(run_id), None

    def _resolve_project(self, target: str) -> ProjectConfig:
        if target in self.config.projects:
            return self.config.projects[target]

        path = Path(target).expanduser().resolve()
        if not path.exists():
            raise AskError(f"Target is neither a configured project nor an existing path: {target}")
        root = self._git_root(path)
        if root is None:
            raise AskError("Ask targets must be inside a git repository.")

        for project in self.config.projects.values():
            if project.path == root:
                return project

        project = discover_project(root, self._generated_project_name(root))
        self.config.projects[project.name] = project
        save_config(self.config, self.config_path)
        return project

    def _generated_project_name(self, root: Path) -> str:
        base = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in root.name).strip("-") or "project"
        digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:8]
        candidate = f"local-{base}-{digest}"
        if candidate not in self.config.projects:
            return candidate
        index = 2
        while f"{candidate}-{index}" in self.config.projects:
            index += 1
        return f"{candidate}-{index}"

    def _git_root(self, path: Path) -> Path | None:
        process = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path if path.is_dir() else path.parent,
            text=True,
            capture_output=True,
            check=False,
        )
        if process.returncode != 0:
            return None
        return Path(process.stdout.strip()).resolve()

    def _title_from_prompt(self, prompt: str) -> str:
        line = " ".join(prompt.strip().split())
        if not line:
            return "Ad hoc Khan task"
        return line[:80]

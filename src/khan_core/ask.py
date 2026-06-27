from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import discover_project, load_config, save_config
from .loop_engine import LoopEngine
from .models import PipelineRecord, ProjectConfig, QueueItemRecord, RunRecord, TaskCapsule, TaskRecord
from .pipeline import PipelineRunner
from .store import Store


class AskError(RuntimeError):
    pass


@dataclass(frozen=True)
class AskOutcome:
    task: TaskRecord
    run: RunRecord | None = None
    item: QueueItemRecord | None = None
    pipeline: PipelineRecord | None = None
    report_path: Path | None = None
    mode: str = "pipeline"


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
        mode: str = "pipeline",
        builder_providers: list[str] | None = None,
    ) -> AskOutcome:
        if mode not in {"pipeline", "single", "queue"}:
            raise AskError("Mode must be one of: pipeline, single, queue")
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
        if enqueue or mode == "queue":
            item = self.store.enqueue_item(
                "pipeline",
                {
                    "task_id": task.id,
                    "planner_provider": "codex",
                    "builder_providers": builder_providers or ["codex", "cursor-agent"],
                },
                priority=priority,
            )
            return AskOutcome(task=task, item=item, mode="queue")
        if mode == "pipeline":
            pipeline = PipelineRunner(self.config_path).run_pipeline(task, builder_providers=builder_providers)
            return AskOutcome(
                task=task,
                pipeline=pipeline,
                report_path=Path(pipeline.report_path) if pipeline.report_path else None,
                mode="pipeline",
            )
        engine = LoopEngine(self.config_path)
        run_id = engine.run_task(task.id)
        return AskOutcome(task=task, run=engine.store.get_run(run_id), mode="single")

    def _resolve_project(self, target: str) -> ProjectConfig:
        if target in self.config.projects and not self._looks_like_path(target):
            return self.config.projects[target]

        path = Path(target).expanduser()
        if self._looks_like_path(target) or path.exists():
            resolved = path.resolve()
            root = self._git_root(resolved)
            if root is None:
                raise AskError("Ask targets must be inside a git repository.")

            for project in self.config.projects.values():
                if project.path == root:
                    return project

            project = discover_project(root, self._generated_project_name(root))
            self.config.projects[project.name] = project
            save_config(self.config, self.config_path)
            return project

        if target in self.config.projects:
            return self.config.projects[target]

        raise AskError(f"Target is neither a configured project nor an existing path: {target}")

    def _looks_like_path(self, target: str) -> bool:
        return target in {".", ".."} or target.startswith(("~", "./", "../")) or os.sep in target or (
            os.altsep is not None and os.altsep in target
        )

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

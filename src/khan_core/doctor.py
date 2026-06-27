from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .models import ConfigFile


def run_doctor(config: ConfigFile) -> list[tuple[str, str]]:
    checks: list[tuple[str, str]] = []
    codex_bin = shutil.which(config.global_config.codex_bin)
    checks.append(("codex_bin", codex_bin or "missing"))
    cursor_agent_bin = shutil.which(config.global_config.cursor_agent_bin)
    checks.append(("cursor_agent_bin", cursor_agent_bin or "missing"))
    git_bin = shutil.which("git")
    checks.append(("git", git_bin or "missing"))
    checks.append(("state_dir", "ok" if config.global_config.state_dir.exists() else "missing"))

    if codex_bin:
        process = subprocess.run([config.global_config.codex_bin, "--version"], text=True, capture_output=True)
        version = process.stdout.strip() or process.stderr.strip() or "unknown"
        checks.append(("codex_version", version))
    if cursor_agent_bin:
        process = subprocess.run([config.global_config.cursor_agent_bin, "--version"], text=True, capture_output=True)
        version = process.stdout.strip() or process.stderr.strip() or "unknown"
        checks.append(("cursor_agent_version", version))

    for name, project in config.projects.items():
        repo_ok = (project.path / ".git").exists()
        checks.append((f"project:{name}", "ok" if repo_ok else f"missing git repo at {project.path}"))
        for command in project.validate_commands:
            checks.append((f"validate:{name}", command))
    return checks

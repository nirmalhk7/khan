from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

import yaml

from .models import ConfigFile, ProjectConfig


REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_CONFIG = """global:
  codex_bin: codex
  cursor_agent_bin: cursor-agent
  state_dir: __STATE_DIR__
  default_profile: default
  max_concurrent_runs: 3
notifications:
  input_needed: true
  say_bin: say
  phrase: Khan needs your input.
profiles:
  default:
    max_iterations: 4
    max_review_rounds: 2
    max_validation_retries: 2
    max_runtime_minutes: 45
    idle_timeout_minutes: 10
    checkpoint_before_run: true
    checkpoint_on_success: false
    auto_review: true
    require_human_approval_after: 2
    stop_on_repeated_failure: true
    stop_on_empty_diff: true
projects: {}
prompts: {}
"""


def expand_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def ensure_state_dir(path: Path) -> None:
    for subdir in ("runs", "logs", "schemas"):
        (path / subdir).mkdir(parents=True, exist_ok=True)


def default_state_dir() -> Path:
    env_home = os.environ.get("KHAN_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()

    home_candidate = Path.home() / ".khan"
    try:
        home_candidate.mkdir(parents=True, exist_ok=True)
        return home_candidate
    except PermissionError:
        return (REPO_ROOT / ".khan").resolve()


def default_config_path() -> Path:
    return default_state_dir() / "config.yaml"


def write_default_config(path: Path | None = None) -> Path:
    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(DEFAULT_CONFIG.replace("__STATE_DIR__", str(config_path.parent)))
    return config_path


def load_config(path: Path | None = None) -> ConfigFile:
    config_path = path or default_config_path()
    if not config_path.exists():
        write_default_config(config_path)
    raw = yaml.safe_load(config_path.read_text()) or {}
    config = ConfigFile.model_validate(raw)
    config.global_config.state_dir = expand_path(config.global_config.state_dir)
    for project in config.projects.values():
        project.path = expand_path(project.path)
    ensure_state_dir(config.global_config.state_dir)
    return config


def save_config(config: ConfigFile, path: Path | None = None) -> Path:
    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    dumped = config.model_dump(by_alias=True, mode="json")
    dumped["global"]["state_dir"] = str(config.global_config.state_dir)
    for project in dumped.get("projects", {}).values():
        project["path"] = str(project["path"])
    config_path.write_text(yaml.safe_dump(dumped, sort_keys=False))
    return config_path


def discover_project(path: Path, name: str) -> ProjectConfig:
    default_branch = detect_default_branch(path)
    validate_commands = detect_validate_commands(path)
    return ProjectConfig(
        name=name,
        path=path,
        default_branch=default_branch,
        validate_commands=validate_commands,
    )


def detect_default_branch(path: Path) -> str:
    head_ref = path / ".git" / "HEAD"
    if head_ref.exists():
        content = head_ref.read_text().strip()
        if content.startswith("ref: refs/heads/"):
            return content.rsplit("/", 1)[-1]
    return "main"


def detect_validate_commands(path: Path) -> list[str]:
    commands: list[str] = []
    package_json = path / "package.json"
    pyproject = path / "pyproject.toml"
    makefile = path / "Makefile"
    cargo_toml = path / "Cargo.toml"

    if package_json.exists():
        try:
            data = json.loads(package_json.read_text())
            scripts = data.get("scripts", {})
            for key in ("test", "lint", "build", "check"):
                if key in scripts:
                    commands.append(f"npm run {key}")
        except json.JSONDecodeError:
            pass

    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text())
            if "tool" in data and "pytest" in data["tool"]:
                commands.append("pytest")
            if "project" in data:
                commands.append("python -m pytest")
        except tomllib.TOMLDecodeError:
            pass

    if cargo_toml.exists():
        commands.extend(["cargo test", "cargo check"])

    if makefile.exists():
        text = makefile.read_text()
        for target in ("test", "lint", "check", "build"):
            if re.search(rf"^{target}:", text, re.MULTILINE):
                commands.append(f"make {target}")

    seen: set[str] = set()
    ordered: list[str] = []
    for command in commands:
        if command not in seen:
            ordered.append(command)
            seen.add(command)
    return ordered

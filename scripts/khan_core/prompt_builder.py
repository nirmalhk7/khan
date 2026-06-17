from __future__ import annotations

import json
from pathlib import Path

from .models import ProjectConfig, TaskCapsule, TaskRecord


WORKER_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "blocked", "needs_review", "needs_human"]},
        "summary": {"type": "string"},
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "tests_run": {"type": "array", "items": {"type": "string"}},
        "open_risks": {"type": "array", "items": {"type": "string"}},
        "next_action": {"type": "string"},
    },
    "required": ["status", "summary", "changed_files", "tests_run", "open_risks", "next_action"],
    "additionalProperties": False,
}


def write_schema(path: Path) -> Path:
    path.write_text(json.dumps(WORKER_SCHEMA, indent=2))
    return path


def build_worker_prompt(
    task: TaskRecord,
    project: ProjectConfig,
    iteration: int,
    prior_failures: list[str],
    review_findings: list[str],
    capsule: TaskCapsule | None = None,
) -> str:
    lines = [
        f"Task title: {task.title}",
        "",
        "Objective:",
        task.prompt,
        "",
        "Success criteria:",
        task.success_criteria,
        "",
        "Repo constraints:",
        f"- Work in project: {project.path}",
        f"- Default branch: {project.default_branch}",
        f"- Sandbox preference: {project.sandbox}",
        f"- Approval policy: {project.approval_policy}",
        "",
        "Validation commands:",
    ]
    if project.validate_commands:
        lines.extend(f"- {command}" for command in project.validate_commands)
    else:
        lines.append("- No validation commands configured; explain what you manually validated.")

    if project.protected_paths:
        lines.extend(["", "Protected paths:", *[f"- {path}" for path in project.protected_paths]])

    if capsule is not None:
        lines.extend(["", "Task capsule:"])
        lines.append(f"- Blast radius: {capsule.blast_radius}")
        if capsule.acceptance_criteria:
            lines.extend(["- Acceptance criteria:", *[f"  - {item}" for item in capsule.acceptance_criteria]])
        if capsule.expected_files:
            lines.extend(["- Expected files:", *[f"  - {item}" for item in capsule.expected_files]])
        if capsule.allowed_paths:
            lines.extend(["- Allowed paths:", *[f"  - {item}" for item in capsule.allowed_paths]])
        if capsule.protected_paths:
            lines.extend(["- Capsule protected paths:", *[f"  - {item}" for item in capsule.protected_paths]])
        if capsule.verification:
            lines.extend(["- Verification recipe:", *[f"  - {item}" for item in capsule.verification]])
        if capsule.conflict_domains:
            lines.extend(["- Conflict domains:", *[f"  - {item}" for item in capsule.conflict_domains]])

    lines.extend(
        [
            "",
            f"Iteration: {iteration}",
            "Keep changes scoped. Stop and report if the repo state is ambiguous or risky.",
        ]
    )

    if prior_failures:
        lines.extend(["", "Prior failures to address:"])
        lines.extend(f"- {failure}" for failure in prior_failures)

    if review_findings:
        lines.extend(["", "Reviewer findings to address:"])
        lines.extend(f"- {finding}" for finding in review_findings)

    lines.extend(
        [
            "",
            "Final response requirements:",
            "- Return JSON matching the provided schema.",
            "- Do not wrap the JSON in markdown fences.",
            "- If blocked, explain exactly what stopped you.",
        ]
    )
    return "\n".join(lines) + "\n"

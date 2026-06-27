from __future__ import annotations

import subprocess
from pathlib import Path

from .models import ValidationResult


class Validator:
    def run(self, workspace: Path, commands: list[str], timeout_seconds: float | None = None) -> ValidationResult:
        if not commands:
            return ValidationResult(ok=True, summary="No validation commands configured.")

        results: list[dict] = []
        failures: list[str] = []
        for command in commands:
            try:
                process = subprocess.run(
                    command, cwd=workspace, shell=True, text=True, capture_output=True,
                    timeout=timeout_seconds, start_new_session=True,
                )
            except subprocess.TimeoutExpired as exc:
                results.append({"command": command, "returncode": 124, "stdout": exc.stdout or "",
                                "stderr": exc.stderr or "validation timed out"})
                failures.append(command)
                continue
            record = {
                "command": command,
                "returncode": process.returncode,
                "stdout": process.stdout,
                "stderr": process.stderr,
            }
            results.append(record)
            if process.returncode != 0:
                failures.append(command)

        ok = not failures
        summary = "Validation passed." if ok else f"Validation failed for: {', '.join(failures)}"
        return ValidationResult(ok=ok, command_results=results, summary=summary)

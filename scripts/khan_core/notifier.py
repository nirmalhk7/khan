from __future__ import annotations

import shutil
import subprocess

from .models import ConfigFile


class InputNotifier:
    def __init__(self, config: ConfigFile) -> None:
        self.config = config

    def notify_input_needed(self, subject_id: str, summary: str, *, subject_type: str) -> bool:
        settings = self.config.notifications
        if not settings.input_needed:
            return False
        say_bin = shutil.which(settings.say_bin)
        if not say_bin:
            return False
        phrase = self._phrase(subject_id, summary, subject_type)
        try:
            subprocess.run([say_bin, phrase], check=False, timeout=15, capture_output=True, text=True)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return True

    def _phrase(self, subject_id: str, summary: str, subject_type: str) -> str:
        settings_phrase = self.config.notifications.phrase.strip()
        message = settings_phrase
        if summary:
            message = f"{settings_phrase} {subject_type} {subject_id[:8]}: {summary}"
        return message

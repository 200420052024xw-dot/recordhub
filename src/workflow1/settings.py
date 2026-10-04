"""Workflow1-owned settings: questionnaire channel token and the deadline rule.

Only Workflow1 consumes these. Future workflows that ask humans to confirm
something must declare their own channel rather than reuse this one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class W1Settings:
    auto_advance_at: str = "12:00"
    confirmation_webhook_token: str = ""

    @classmethod
    def from_env(cls) -> "W1Settings":
        return cls(
            auto_advance_at=os.getenv("RECORDHUB_AUTO_ADVANCE_AT", "12:00").strip(),
            confirmation_webhook_token=os.getenv(
                "RECORDHUB_CONFIRMATION_WEBHOOK_TOKEN", ""
            ),
        )

    def deadline_parts(self) -> tuple[int, int] | None:
        """Hour/minute for the scheduler, or None when auto-advance is off."""
        if not self.auto_advance_at:
            return None
        hour, minute = (int(value) for value in self.auto_advance_at.split(":", 1))
        return hour, minute

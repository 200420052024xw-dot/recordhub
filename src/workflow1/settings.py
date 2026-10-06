"""Workflow1-owned settings: questionnaire token and scheduled deadline.

Only Workflow1 consumes these. Future workflows that ask humans to confirm
something must declare their own channel rather than reuse this one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from config.schedules import ScheduleConfig


@dataclass(frozen=True, slots=True)
class W1Settings:
    auto_advance_at: str
    confirmation_webhook_token: str = ""
    notify_at: str = "08:00"
    minister_review_form_url: str = ""
    backbone_review_form_url: str = ""

    @classmethod
    def from_schedule(cls, schedules: ScheduleConfig) -> "W1Settings":
        daily = schedules.workflows["workflow1_daily"]
        if "auto_advance_at" not in daily.options:
            raise ValueError("workflow1_daily 缺少 auto_advance_at 截止时间")
        for key in ("minister_review_form_url", "backbone_review_form_url"):
            if not str(daily.options.get(key, "")).strip():
                raise ValueError(f"workflow1_daily 缺少 {key} 审核表单链接")
        return cls(
            auto_advance_at=str(daily.options["auto_advance_at"]).strip(),
            notify_at=str(daily.options.get("notify_at", "08:00")).strip(),
            minister_review_form_url=str(daily.options.get("minister_review_form_url", "")).strip(),
            backbone_review_form_url=str(daily.options.get("backbone_review_form_url", "")).strip(),
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

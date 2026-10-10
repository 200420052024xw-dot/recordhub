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
    report_notify_at: str = "22:00"
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
            report_notify_at=str(daily.options.get("report_notify_at", "22:00")).strip(),
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


@dataclass(frozen=True, slots=True)
class LogSubmitSettings:
    """对话入口提交工作日志的窗口与兜底表单。

    与 W1Settings 分开:这是给学生的提交入口,不是人工评价通道。
    提交窗口写在 schedules.toml 的 daily_log_reminder 一节。
    """

    submit_opens_at: str
    submit_closes_at: str
    fallback_form_url: str
    archive_at: str

    @classmethod
    def from_schedule(cls, schedules: ScheduleConfig) -> "LogSubmitSettings":
        reminder = schedules.workflows.get("daily_log_reminder")
        if reminder is None:
            raise ValueError("schedules.toml 缺少 daily_log_reminder 配置")
        opens = str(reminder.options.get("submit_opens_at", "")).strip()
        closes = str(reminder.options.get("submit_closes_at", "")).strip()
        if not opens or not closes:
            raise ValueError(
                "daily_log_reminder 缺少 submit_opens_at / submit_closes_at 提交时间窗")
        form_url = str(reminder.options.get("form_url", "")).strip()
        if not form_url:
            raise ValueError("daily_log_reminder 缺少 form_url 兜底表单链接")
        daily = schedules.workflows.get("workflow1_daily")
        if daily is None:
            raise ValueError("schedules.toml 缺少 workflow1_daily 配置")
        return cls(
            submit_opens_at=opens,
            submit_closes_at=closes,
            fallback_form_url=form_url,
            archive_at=daily.time,
        )

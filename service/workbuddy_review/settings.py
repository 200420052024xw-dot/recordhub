"""批改接口的配置：接口截止时间。

和 W1Settings / LogSubmitSettings 分开：这是给老师的批改入口，不是学生提交入口。
截止时间写在 schedules.toml 的 workbuddy_review 一节。
"""

from __future__ import annotations

from dataclasses import dataclass

from config.schedules import ScheduleConfig


@dataclass(frozen=True, slots=True)
class ReviewSettings:
    closes_at: str

    @classmethod
    def from_schedule(cls, schedules: ScheduleConfig) -> "ReviewSettings":
        section = schedules.workflows.get("workbuddy_review")
        if section is None:
            raise ValueError("schedules.toml 缺少 workbuddy_review 配置")
        closes_at = str(section.time).strip()
        if not closes_at:
            raise ValueError("workbuddy_review 缺少接口截止时间")
        return cls(closes_at=closes_at)

    def close_parts(self) -> tuple[int, int]:
        """Hour/minute for computing the deadline."""
        hour, minute = (int(value) for value in self.closes_at.split(":", 1))
        return hour, minute

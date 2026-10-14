"""把「昨天 / 上周」这类说法解析成日期区间，并卡上限、收敛今天。

时区由调用方以 ``today`` 注入（上海时区的当天），本模块只做纯日期运算。
"""

from __future__ import annotations

from datetime import date, timedelta

from service.workbuddy_query.models import (
    RANGE_INVALID,
    RANGE_TOO_LARGE,
    QueryError,
    RangeSpec,
)

SHORTCUTS = frozenset({
    "today", "yesterday", "day_before_yesterday",
    "this_week", "last_week", "last_7_days", "last_30_days",
    "this_month", "last_month",
})
MAX_WINDOW_DAYS = 31


def _shortcut(name: str, today: date) -> tuple[date, date]:
    if name == "today":
        return today, today
    if name == "yesterday":
        day = today - timedelta(days=1)
        return day, day
    if name == "day_before_yesterday":
        day = today - timedelta(days=2)
        return day, day
    if name == "this_week":
        return today - timedelta(days=today.weekday()), today
    if name == "last_week":
        monday = today - timedelta(days=today.weekday())
        return monday - timedelta(days=7), monday - timedelta(days=1)
    if name == "last_7_days":
        return today - timedelta(days=6), today
    if name == "last_30_days":
        return today - timedelta(days=29), today
    if name == "this_month":
        return today.replace(day=1), today
    if name == "last_month":
        last_day = today.replace(day=1) - timedelta(days=1)
        return last_day.replace(day=1), last_day
    raise QueryError(RANGE_INVALID)


def resolve_window(spec: RangeSpec | None, *, today: date) -> tuple[date, date]:
    """解析出 ``(start, end)``；范围问题一律报 ``RANGE_INVALID`` / ``RANGE_TOO_LARGE``。"""
    if spec is None:
        raise QueryError(RANGE_INVALID)
    shortcut = (spec.shortcut or "").strip()
    has_explicit = spec.from_ is not None or spec.to is not None
    if shortcut and has_explicit:
        raise QueryError(RANGE_INVALID)
    if shortcut:
        start, end = _shortcut(shortcut, today)
    elif spec.from_ is not None and spec.to is not None:
        start, end = spec.from_, spec.to
    else:
        raise QueryError(RANGE_INVALID)
    if start > end:
        raise QueryError(RANGE_INVALID)
    if (end - start).days + 1 > MAX_WINDOW_DAYS:
        raise QueryError(RANGE_TOO_LARGE)
    return start, end


def clamp_view_window(start: date, end: date, *, today: date) -> tuple[date, date]:
    """查看不含今天：把 ``end`` 收敛到昨天；收敛后为空则报 ``RANGE_INVALID``。"""
    end = min(end, today - timedelta(days=1))
    if start > end:
        raise QueryError(RANGE_INVALID)
    return start, end

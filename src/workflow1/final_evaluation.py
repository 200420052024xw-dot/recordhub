"""Merge a log's evaluation units into the single 最终评价 line.

Order is fixed by ``(first department, evaluator)`` so reruns are stable.
Human-confirmed units win; missing human text falls back to the log's single
AI evaluation, marked 「（AI未确认）」, written only once per log even when
several units lack human input. AI failure renders 「评价生成失败」.
"""

from __future__ import annotations

import re

from workflow1.models import DailySnapshot
from workflow1.units import effective_text, units_for_log

AI_UNCONFIRMED_MARK = "（AI未确认）"
FAILED_TEXT = "评价生成失败"
_WHITESPACE = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _segment(positive: str, improvement: str, *, ai_mark: bool) -> str:
    segment = f"肯定之处：{_clean(positive)}；改进之处：{_clean(improvement)}"
    return f"{segment}{AI_UNCONFIRMED_MARK}" if ai_mark else segment


def render_final_evaluation(snapshot: DailySnapshot, log_id: str) -> str:
    units = units_for_log(snapshot, log_id)
    if not units:
        return ""
    segments: list[str] = []
    ai_used = False
    for unit in units:
        source, positive, improvement = effective_text(snapshot, unit)
        if source == "HUMAN":
            segments.append(_segment(positive, improvement, ai_mark=False))
            continue
        # 缺人工：整条日志只补一段 AI（AI 一条日志一份，不按缺失人数重复）。
        if ai_used:
            continue
        ai_used = True
        if source == "AI":
            segments.append(_segment(positive, improvement, ai_mark=True))
        else:
            segments.append(FAILED_TEXT)
    return "；".join(segments)

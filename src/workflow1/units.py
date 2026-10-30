"""Helpers for the v3 evaluation-unit snapshot.

AI evaluation is one per log; human evaluation is one per
``(log_id, evaluator_id)``.  A unit can cover several departments when the
same evaluator evaluates the same person in more than one department.
"""

from __future__ import annotations

from schema import Organization
from workflow1.models import AiEvaluationState, DailySnapshot, LogEvaluationState


def unit_key(log_id: str, evaluator_id: str) -> str:
    return f"{log_id}:{evaluator_id}"


def parse_unit_key(key: str) -> tuple[str, str] | None:
    log_id, separator, evaluator_id = key.rpartition(":")
    if not separator or not log_id or not evaluator_id:
        return None
    return log_id, evaluator_id


def ai_for(snapshot: DailySnapshot, log_id: str) -> AiEvaluationState | None:
    return snapshot.ai_evaluations.get(log_id)


def units_for_log(snapshot: DailySnapshot, log_id: str) -> list[LogEvaluationState]:
    return sorted(
        (unit for unit in snapshot.evaluation_units.values()
         if unit.log_id == log_id),
        key=lambda unit: (min(unit.department_ids or [""]), unit.evaluator_id),
    )


def units_for_evaluator(snapshot: DailySnapshot,
                        evaluator_id: str) -> list[LogEvaluationState]:
    progress = snapshot.evaluators.get(evaluator_id)
    if progress is not None and progress.unit_keys:
        keys = progress.unit_keys
    else:
        keys = [unit_key(unit.log_id, unit.evaluator_id)
                for unit in snapshot.evaluation_units.values()
                if unit.evaluator_id == evaluator_id]
    result = []
    for key in keys:
        unit = snapshot.evaluation_units.get(key)
        if unit is not None and unit.evaluator_id == evaluator_id:
            result.append(unit)
    return result


def units_closed_for(snapshot: DailySnapshot, evaluator_id: str,
                     department_id: str) -> bool:
    """True when every unit this evaluator owns in the department is confirmed."""
    units = [unit for unit in snapshot.evaluation_units.values()
             if unit.evaluator_id == evaluator_id
             and department_id in unit.department_ids]
    return all(unit.status.value == "CONFIRMED" for unit in units)


def ai_text(snapshot: DailySnapshot, log_id: str) -> tuple[str, str] | None:
    ai = ai_for(snapshot, log_id)
    if ai is None or ai.fallback == "failed":
        return None
    positive = (ai.positive_ai or "").strip()
    improvement = (ai.improvement_ai or "").strip()
    if not positive or not improvement:
        return None
    return positive, improvement


def effective_text(snapshot: DailySnapshot,
                   unit: LogEvaluationState) -> tuple[str, str, str]:
    """Return ``(source, positive, improvement)`` for one evaluation unit."""
    positive = (unit.positive_final or "").strip()
    improvement = (unit.improvement_final or "").strip()
    if unit.source == "HUMAN" and unit.status.value == "CONFIRMED" and positive and improvement:
        return "HUMAN", positive, improvement
    fallback = ai_text(snapshot, unit.log_id)
    if fallback is not None:
        return "AI", fallback[0], fallback[1]
    return "FAILED", "", ""


def evaluation_table_for(unit: LogEvaluationState,
                         organization: Organization) -> str:
    """Pick the human form table from the subject's role in the unit's scope."""
    person = organization.person_map().get(unit.person_id)
    roles = set()
    if person is not None:
        for department_id in unit.department_ids:
            membership = organization.membership(unit.person_id, department_id)
            if membership is not None:
                roles.add(membership.role)
        if not roles:
            roles.add(person.role)
    if roles == {"骨干学生"}:
        return "human_evaluations"
    if roles == {"基层学生"}:
        return "human_evaluations_backbone"
    if not roles:
        return "human_evaluations_backbone"
    raise ValueError(
        f"评价单元 {unit.log_id} 的被评价人「{unit.person_id}」在多个部门角色不同，"
        "无法选择人工评价表")

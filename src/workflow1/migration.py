"""In-memory migration from snapshot schema v2 to v3."""

from __future__ import annotations

from workflow1.models import (
    AiEvaluationState,
    DailySnapshot,
    EvaluatorProgress,
    LogEvaluationState,
)
from workflow1.units import unit_key


def migrate_snapshot(snapshot: DailySnapshot) -> DailySnapshot:
    if snapshot.schema_version >= 3:
        return snapshot
    people = snapshot.organization.person_map()
    units: dict[str, LogEvaluationState] = {}
    ai_evaluations: dict[str, AiEvaluationState] = {}
    for log_id, state in snapshot.log_evaluations.items():
        ai_evaluations[log_id] = AiEvaluationState(
            log_id=log_id,
            status=state.status,
            positive_ai=state.positive_ai,
            improvement_ai=state.improvement_ai,
            record_id=state.record_id,
            evaluation_id=state.evaluation_id,
            evaluated_at=state.evaluated_at,
            ai_evaluated_at=state.ai_evaluated_at,
            prompt_source=state.prompt_source,
            prompt_record_id=state.prompt_record_id,
            error=state.error,
            retry_count=state.retry_count,
            retry_after=state.retry_after,
            fallback=state.fallback,
        )
        if not state.evaluator_id:
            continue
        person = people.get(state.person_id)
        department_ids = []
        if person is not None:
            department_ids = [item.department_id for item in person.membership_list()
                              if item.leader_id == state.evaluator_id]
            if not department_ids and person.department_id:
                department_ids = [person.department_id]
        units[unit_key(log_id, state.evaluator_id)] = LogEvaluationState(
            log_id=log_id,
            person_id=state.person_id,
            evaluator_id=state.evaluator_id,
            department_ids=sorted(set(department_ids)),
            status=state.status,
            positive_final=(state.positive_final
                            if state.source == "HUMAN" else None),
            improvement_final=(state.improvement_final
                               if state.source == "HUMAN" else None),
            source=state.source,
            manual_skipped=state.manual_skipped,
            evaluated_at=state.evaluated_at,
            confirmed_at=state.confirmed_at,
            error=state.error,
            fallback=state.fallback,
        )
    evaluators: dict[str, EvaluatorProgress] = {}
    for unit in units.values():
        progress = evaluators.setdefault(
            unit.evaluator_id, EvaluatorProgress(evaluator_id=unit.evaluator_id))
        key = unit_key(unit.log_id, unit.evaluator_id)
        if key not in progress.unit_keys:
            progress.unit_keys.append(key)
        if unit.log_id not in progress.log_ids:
            progress.log_ids.append(unit.log_id)
    for evaluator_id, old in snapshot.evaluators.items():
        progress = evaluators.setdefault(
            evaluator_id, EvaluatorProgress(evaluator_id=evaluator_id))
        progress.notified = old.notified
        progress.closed = old.closed
        for log_id in old.log_ids:
            key = unit_key(log_id, evaluator_id)
            if key in units and key not in progress.unit_keys:
                progress.unit_keys.append(key)
            if log_id not in progress.log_ids:
                progress.log_ids.append(log_id)
    for progress in evaluators.values():
        progress.unit_keys.sort()
        progress.log_ids.sort()
    return snapshot.model_copy(update={
        "schema_version": 3,
        "log_evaluations": {},
        "ai_evaluations": ai_evaluations,
        "evaluation_units": units,
        "evaluators": evaluators,
        "final_evaluations": {},
        "final_evaluations_written_at": {},
    })

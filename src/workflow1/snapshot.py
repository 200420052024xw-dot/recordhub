"""Freeze one day's complete source material and evaluation ownership."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from schema import Organization, SubmissionStatus, WorkLog
from workflow1.models import (
    AiEvaluationState,
    DailySnapshot,
    EvaluatorProgress,
    LogEvaluationState,
)
from workflow1.units import unit_key


class SnapshotBuilder:
    roles = {"基层学生": "骨干学生", "骨干学生": "部长"}

    def build(self, *, workflow_run_id: str, target_date: date,
              organization: Organization, logs: list[WorkLog],
              now: datetime) -> DailySnapshot:
        people = organization.person_map()
        if len(people) != len(organization.persons):
            raise ValueError("人员编号重复，不能冻结当日组织关系")
        log_ids = [log.log_id for log in logs]
        if len(set(log_ids)) != len(log_ids):
            raise ValueError("同一天存在重复日志编号")
        for log in logs:
            if ":" in log.log_id:
                raise ValueError(f"日志编号不能包含冒号：{log.log_id}")
        logs_by_person: dict[str, list[WorkLog]] = defaultdict(list)
        for log in logs:
            if log.person_id not in people:
                raise ValueError(f"日志 {log.log_id} 的提交人不在当日人员快照")
            logs_by_person[log.person_id].append(log)

        # (person_id, evaluator_id) -> department ids and roles seen
        expected: dict[tuple[str, str], list[str]] = defaultdict(list)
        unit_roles: dict[tuple[str, str], set[str]] = defaultdict(set)
        for person in organization.persons:
            if not person.active:
                continue
            if ":" in person.person_id:
                raise ValueError(f"人员编号不能包含冒号：{person.person_id}")
            if not any(item.role in self.roles for item in person.membership_list()):
                continue
            for membership in person.membership_list():
                if membership.role not in self.roles:
                    continue
                if not membership.department_id:
                    raise ValueError(f"人员 {person.person_id} 缺少部门")
                leader = people.get(membership.leader_id or "")
                expected_role = self.roles[membership.role]
                if (not leader or not leader.active
                        or leader.person_id == person.person_id):
                    raise ValueError(f"人员 {person.person_id} 缺少有效直属评价人")
                leader_membership = organization.membership(
                    leader.person_id, membership.department_id)
                if leader_membership is None or leader_membership.role != expected_role:
                    raise ValueError(f"人员 {person.person_id} 缺少有效直属评价人")
                key = (person.person_id, leader.person_id)
                if membership.department_id not in expected[key]:
                    expected[key].append(membership.department_id)
                unit_roles[key].add(membership.role)

        units: dict[str, LogEvaluationState] = {}
        ai_evaluations: dict[str, AiEvaluationState] = {}
        submitted_ids: set[str] = set()
        for person in organization.persons:
            person_logs = logs_by_person.get(person.person_id, [])
            person_units: list[tuple[str, list[str]]] = []
            for (person_id, evaluator_id), department_ids in expected.items():
                if person_id != person.person_id:
                    continue
                if len(unit_roles[(person_id, evaluator_id)]) > 1:
                    raise ValueError(
                        f"人员 {person.person_id} 与评价人 {evaluator_id} 在多个部门的"
                        "被评价角色不同，不能共用同一条评价")
                person_units.append((evaluator_id, sorted(department_ids)))
            if person_units:
                submitted_ids.add(person.person_id)
            for log in person_logs:
                for evaluator_id, department_ids in person_units:
                    key = unit_key(log.log_id, evaluator_id)
                    units[key] = LogEvaluationState(
                        log_id=log.log_id,
                        person_id=person.person_id,
                        evaluator_id=evaluator_id,
                        department_ids=department_ids,
                    )
                if person_units:
                    ai_evaluations[log.log_id] = AiEvaluationState(log_id=log.log_id)

        submission_status = {
            person_id: SubmissionStatus(
                person_id=person_id,
                submitted=bool(logs_by_person.get(person_id)),
                log_id=(logs_by_person[person_id][-1].log_id
                        if logs_by_person.get(person_id) else None),
                log_ids=sorted(log.log_id for log in logs_by_person.get(person_id, [])),
            )
            for person_id in submitted_ids
        }

        evaluators: dict[str, EvaluatorProgress] = {}
        for unit in units.values():
            progress = evaluators.setdefault(
                unit.evaluator_id,
                EvaluatorProgress(evaluator_id=unit.evaluator_id))
            if unit_key(unit.log_id, unit.evaluator_id) not in progress.unit_keys:
                progress.unit_keys.append(unit_key(unit.log_id, unit.evaluator_id))
            if unit.log_id not in progress.log_ids:
                progress.log_ids.append(unit.log_id)
        for progress in evaluators.values():
            progress.unit_keys.sort()
            progress.log_ids.sort()

        return DailySnapshot(
            workflow_run_id=workflow_run_id, target_date=target_date,
            organization=organization, logs=logs,
            submission_status=submission_status,
            ai_evaluations=ai_evaluations,
            evaluation_units=units,
            evaluators=evaluators,
            created_at=now, updated_at=now)

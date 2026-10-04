"""Freeze one day's complete source material and evaluation ownership."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from schema import Organization, SubmissionStatus, WorkLog
from workflow1.models import DailySnapshot, EvaluatorProgress, LogEvaluationState


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
        logs_by_person: dict[str, list[WorkLog]] = defaultdict(list)
        for log in logs:
            if log.person_id not in people:
                raise ValueError(f"日志 {log.log_id} 的提交人不在当日人员快照")
            logs_by_person[log.person_id].append(log)

        ownership: dict[str, str] = {}
        for person in organization.persons:
            if not person.active or person.role not in self.roles:
                continue
            leader = people.get(person.leader_id or "")
            if (not leader or not leader.active
                or leader.role != self.roles[person.role]
                or leader.department_id != person.department_id):
                raise ValueError(f"人员 {person.person_id} 缺少有效直属评价人")
            if not person.department_id:
                raise ValueError(f"人员 {person.person_id} 缺少部门")
            ownership[person.person_id] = leader.person_id

        submissions = {
            person_id: SubmissionStatus(
                person_id=person_id, submitted=bool(logs_by_person[person_id]),
                log_id=logs_by_person[person_id][-1].log_id
                    if logs_by_person[person_id] else None,
                log_ids=sorted(log.log_id for log in logs_by_person[person_id]),
            )
            for person_id in ownership
        }
        evaluations = {
            log.log_id: LogEvaluationState(
                log_id=log.log_id, person_id=log.person_id,
                evaluator_id=ownership.get(log.person_id, ""))
            for log in logs
        }
        evaluators: dict[str, EvaluatorProgress] = {}
        for evaluator_id in sorted(set(ownership.values())):
            evaluators[evaluator_id] = EvaluatorProgress(
                evaluator_id=evaluator_id,
                log_ids=sorted(log_id for log_id, state in evaluations.items()
                               if state.evaluator_id == evaluator_id))
        return DailySnapshot(
            workflow_run_id=workflow_run_id, target_date=target_date,
            organization=organization, logs=logs,
            submission_status=submissions,
            log_evaluations=evaluations, evaluators=evaluators,
            created_at=now, updated_at=now)

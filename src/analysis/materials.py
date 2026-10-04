"""Prepare complete, authorized date ranges from original daily check snapshots."""

from datetime import date, timedelta

from data.store import FileStateStore
from workflow1.models import UnitStatus
from analysis.models import MaterialStatistics, PreparedMaterial
from skills import AvailableResource, EvaluationText, MaterialScope, SkillInput, TextRecord


class MaterialPreparer:
    def __init__(self, store: FileStateStore):
        self.store = store
        self.cache: dict[tuple, PreparedMaterial] = {}

    def prepare(self, *, run_id: str, start: date, end: date,
                department_ids: list[str]) -> PreparedMaterial:
        if end < start:
            raise ValueError("分析起止日期倒置")
        department_ids = sorted(set(department_ids))
        dates = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
        signatures = []
        for day in dates:
            path = self.store.runs_dir / f"{day}.json"
            if not path.exists():
                path = self.store.checked_dir / f"{day}.json"
            stat = path.stat() if path.exists() else None
            signatures.append((day, stat.st_mtime_ns if stat else None, stat.st_size if stat else None))
        cache_key = (start, end, tuple(department_ids), tuple(signatures))
        if cache_key in self.cache:
            cached = self.cache[cache_key].model_copy(deep=True)
            cached.input.run_id = run_id
            return cached
        records: dict[str, TextRecord] = {}
        resources: dict[str, AvailableResource] = {}
        missing = []
        expected, submitted = set(), set()
        for day in dates:
            snapshot = self.store.load_check_snapshot(day)
            if snapshot is None:
                missing.append(f"{day} 每日检查快照缺失")
                continue
            if snapshot.schema_version != 2:
                missing.append(f"{day} 每日检查快照需迁移")
                continue
            people = snapshot.organization.person_map()
            departments = snapshot.organization.department_map()
            eligible = sorted((person for person in people.values()
                if person.active and person.department_id in department_ids
                and person.role in {"基层学生", "骨干学生"}), key=lambda person: person.person_id)
            for person in eligible:
                expected.add((day, person.person_id))
                status = snapshot.submission_status.get(person.person_id)
                if status is None:
                    missing.append(f"{day} {person.person_id} 缺少提交核算")
                    continue
                logs = sorted((log for log in snapshot.logs if log.person_id == person.person_id),
                              key=lambda log: (log.submitted_at, log.log_id))
                if bool(logs) != status.submitted or set(status.log_ids) != {log.log_id for log in logs}:
                    missing.append(f"{day} {person.person_id} 日志与提交核算不一致")
                    continue
                common = dict(person_id=person.person_id, name=person.name,
                    department_id=person.department_id,
                    department_name=departments[person.department_id].name,
                    role=person.role, work_start=day, work_end=day)
                if not logs:
                    record = TextRecord(record_id=f"missing:{day}:{person.person_id}",
                                        submitted=False, confirmed=True, **common)
                    records[record.record_id] = record
                    continue
                submitted.add((day, person.person_id))
                for log in logs:
                    state = snapshot.log_evaluations.get(log.log_id)
                    if state is None or state.status != UnitStatus.CONFIRMED:
                        missing.append(f"{day} 日志 {log.log_id} 尚未完成评价确认")
                        continue
                    if (state.person_id != person.person_id or state.log_id != log.log_id
                            or state.evaluator_id != person.leader_id):
                        missing.append(f"{day} 日志 {log.log_id} 的评价身份与当日组织关系不一致")
                        continue
                    if not state.positive_final or not state.improvement_final:
                        missing.append(f"{day} 日志 {log.log_id} 缺少最终评价")
                        continue
                    evaluator = people.get(state.evaluator_id)
                    if evaluator is None:
                        missing.append(f"{day} 日志 {log.log_id} 评价人缺失")
                        continue
                    reviews = []
                    positive_ai = state.positive_ai or (state.positive_final if state.source == "AI" else "")
                    improvement_ai = state.improvement_ai or (state.improvement_final if state.source == "AI" else "")
                    if positive_ai or improvement_ai:
                        reviews.append(EvaluationText(evaluator_id=evaluator.person_id,
                            evaluator_role=evaluator.role, evaluated_at=state.ai_evaluated_at or (state.evaluated_at if state.source == "AI" else None),
                            positive=positive_ai, improvement=improvement_ai, source="AI"))
                    if state.source == "HUMAN":
                        reviews.append(EvaluationText(evaluator_id=evaluator.person_id,
                            evaluator_role=evaluator.role, evaluated_at=state.confirmed_at or state.evaluated_at,
                            positive=state.positive_final, improvement=state.improvement_final, source="HUMAN"))
                    record = TextRecord(record_id=log.source_record_id or log.log_id,
                        confirmed=True, progress=log.progress, difficulties=log.difficulties,
                        reflection=log.reflection, other=log.other, full_log=log.full_log,
                        achievement_refs=log.achievement_refs, evaluations=reviews, **common)
                    existing = records.get(record.record_id)
                    if existing and existing != record:
                        raise ValueError(f"原日志 {record.record_id} 在范围内出现冲突版本")
                    records[record.record_id] = record
                    for resource in log.resources:
                        previous = resources.get(resource.resource_id)
                        if previous:
                            if (previous.name, previous.category) != (resource.name, resource.category):
                                missing.append(f"资源 {resource.resource_id} 在原材料中定义冲突")
                                continue
                            if record.record_id not in previous.source_record_ids:
                                previous.source_record_ids.append(record.record_id)
                            descriptions = set(previous.description.split("\n"))
                            descriptions.add(resource.description)
                            previous.description = "\n".join(sorted(filter(None, descriptions)))
                        else:
                            resources[resource.resource_id] = AvailableResource(
                                **resource.model_dump(), source_record_ids=[record.record_id])
        expected_people = {person for _, person in expected}
        submitted_people = {person for _, person in submitted}
        stats = MaterialStatistics(expected_people=len(expected_people),
            submitted_people=len(submitted_people), missing_people=len(expected_people - submitted_people),
            expected_person_days=len(expected), submitted_person_days=len(submitted),
            missing_person_days=len(expected - submitted))
        prepared = PreparedMaterial(input=SkillInput(run_id=run_id,
            scope=MaterialScope(start_date=start, end_date=end, department_ids=department_ids,
                complete=not missing, missing_sources=missing),
            records=sorted(records.values(), key=lambda record: (record.work_start, record.person_id, record.record_id)),
            resources=sorted(resources.values(), key=lambda resource: resource.resource_id)),
            statistics=stats if not missing else None)
        if not missing:
            # Bounded cache: multiple Skills share a prepared range, not the whole history.
            if len(self.cache) >= 32:
                self.cache.pop(next(iter(self.cache)))
            self.cache[cache_key] = prepared.model_copy(deep=True)
        return prepared

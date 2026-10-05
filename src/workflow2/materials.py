"""Prepare authorized date ranges from original daily check snapshots."""

from datetime import date, timedelta

from data.store import FileStateStore
from workflow1.models import UnitStatus
from workflow2.models import MaterialStatistics, PreparedMaterial
from workflow2.skills import AvailableResource, EvaluationText, MaterialScope, SkillInput, TextRecord


class MaterialPreparer:
    def __init__(self, store: FileStateStore, rebuilder=None):
        self.store = store
        self.rebuilder = rebuilder
        self.cache: dict[tuple, PreparedMaterial] = {}
        self._rebuild_cache: dict[tuple, dict] = {}

    def prepare(self, *, run_id: str, start: date, end: date,
                department_ids: list[str], person_ids: list[str] | None = None,
                allow_rebuild: bool = False,
                allow_partial: bool = False) -> PreparedMaterial:
        if end < start:
            raise ValueError("分析起止日期倒置")
        department_ids = sorted(set(department_ids))
        member_filter = sorted(set(person_ids)) if person_ids is not None else None
        dates = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
        signatures = []
        for day in dates:
            path = self.store.runs_dir / f"{day}.json"
            if not path.exists():
                path = self.store.checked_dir / f"{day}.json"
            stat = path.stat() if path.exists() else None
            signatures.append((day, stat.st_mtime_ns if stat else None, stat.st_size if stat else None))
        cache_key = (start, end, allow_partial, tuple(department_ids),
                     tuple(member_filter or ()), tuple(signatures))
        # Rebuilt material has no file signature; skip the prepared cache for it.
        if cache_key in self.cache and not allow_rebuild:
            cached = self.cache[cache_key].model_copy(deep=True)
            cached.input.run_id = run_id
            return cached
        records: dict[str, TextRecord] = {}
        resources: dict[str, AvailableResource] = {}
        missing = []
        expected, submitted = set(), set()
        rebuilt: dict | None = None
        for day in dates:
            snapshot = self.store.load_check_snapshot(day)
            if snapshot is None and allow_rebuild and self.rebuilder is not None:
                if rebuilt is None:
                    window = (start, end)
                    if window not in self._rebuild_cache:
                        self._rebuild_cache[window] = self.rebuilder.rebuild(start, end)
                    rebuilt = self._rebuild_cache[window]
                snapshot = rebuilt.get(day)
            if snapshot is None:
                missing.append(f"{day} 每日检查快照缺失")
                continue
            if snapshot.schema_version != 2:
                missing.append(f"{day} 每日检查快照需迁移")
                continue
            people = snapshot.organization.person_map()
            departments = snapshot.organization.department_map()
            member_set = set(member_filter) if member_filter else None
            eligible = sorted((person for person in people.values()
                if person.active and person.department_id in department_ids
                and person.role in {"基层学生", "骨干学生"}
                and (member_set is None or person.person_id in member_set)),
                key=lambda person: person.person_id)
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
                    # Only the effective (finalized) evaluation reaches the Skills:
                    # the human text when the evaluator edited it, otherwise the AI text.
                    if state.source == "HUMAN":
                        reviews = [EvaluationText(evaluator_id=evaluator.person_id,
                            evaluator_role=evaluator.role,
                            evaluated_at=state.confirmed_at or state.evaluated_at,
                            positive=state.positive_final or "", improvement=state.improvement_final or "",
                            source="HUMAN")]
                    else:
                        reviews = [EvaluationText(evaluator_id=evaluator.person_id,
                            evaluator_role=evaluator.role,
                            evaluated_at=state.ai_evaluated_at or state.evaluated_at,
                            positive=state.positive_ai or state.positive_final or "",
                            improvement=state.improvement_ai or state.improvement_final or "",
                            source="AI")]
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
                complete=allow_partial or not missing,
                missing_sources=[] if allow_partial else missing,
                omitted_sources=missing if allow_partial else []),
            records=sorted(records.values(), key=lambda record: (record.work_start, record.person_id, record.record_id)),
            resources=sorted(resources.values(), key=lambda resource: resource.resource_id)),
            statistics=stats if allow_partial or not missing else None)
        if not missing and not allow_rebuild:
            # Bounded cache: multiple Skills share a prepared range, not the whole history.
            if len(self.cache) >= 32:
                self.cache.pop(next(iter(self.cache)))
            self.cache[cache_key] = prepared.model_copy(deep=True)
        return prepared

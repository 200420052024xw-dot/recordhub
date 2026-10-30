"""Prepare authorized date ranges from original daily check snapshots."""

from datetime import date, timedelta

from data.store import FileStateStore
from workflow1.models import UnitStatus
from workflow1.units import effective_text, units_for_log
from workflow2.models import MaterialStatistics, PreparedMaterial
from workflow2.skills import AvailableResource, EvaluationText, MaterialScope, SkillInput, TextRecord


def _without_placeholders(reviews: list[EvaluationText],
                          fallback: str) -> list[EvaluationText]:
    """降级/失败留下的占位文案是给填表人看的指路话，不是评价内容，不能进分析材料。

    "unparsed"：模型确实产出了内容（原文留在肯定之处），只把那句"详情见肯定之处_AI"去掉。
    "failed"：整条评价就是一句"请人工补评"，没有内容可给，按"这段没有评价"处理。
    """
    if fallback == "failed":
        return []
    if fallback == "unparsed":
        return [review.model_copy(update={"improvement": ""}) for review in reviews]
    return reviews


class MaterialPreparer:
    def __init__(self, store: FileStateStore, rebuilder=None):
        self.store = store
        self.rebuilder = rebuilder
        self.cache: dict[tuple, PreparedMaterial] = {}
        self._rebuild_cache: dict[tuple, dict] = {}

    def prepare(self, *, run_id: str, start: date, end: date,
                department_ids: list[str], person_ids: list[str] | None = None,
                allow_rebuild: bool = False,
                allow_partial: bool = False,
                with_evaluations: bool = True) -> PreparedMaterial:
        """with_evaluations=False 给只汇总日志原文的技能（S04）：材料不带评价，也不等评价定稿。"""
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
        cache_key = (start, end, allow_partial, with_evaluations, tuple(department_ids),
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
            if snapshot.schema_version != 3:
                missing.append(f"{day} 每日检查快照需迁移")
                continue
            people = snapshot.organization.person_map()
            departments = snapshot.organization.department_map()
            member_set = set(member_filter) if member_filter else None
            team_scope = len(department_ids) > 1
            selected: list[tuple[object, object]] = []
            for person in sorted(people.values(), key=lambda item: item.person_id):
                if not person.active:
                    continue
                if member_set is not None and person.person_id not in member_set:
                    continue
                memberships = [item for item in person.membership_list()
                               if item.department_id in department_ids
                               and item.role in {"基层学生", "骨干学生"}]
                if not memberships:
                    continue
                # 每次 prepare 内每人每天只能生成一条记录，避免 record_id 冲突；
                # 多部门批次优先主部门，否则取排序后的第一个部门。
                membership = (next((item for item in memberships if item.primary),
                                   sorted(memberships, key=lambda item: item.department_id)[0])
                              if team_scope else memberships[0])
                selected.append((person, membership))
            for person, membership in selected:
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
                department = departments.get(membership.department_id)
                common = dict(person_id=person.person_id, name=person.name,
                    department_id=membership.department_id,
                    department_name=(department.name if department
                                     else membership.department_id),
                    role=membership.role, work_start=day, work_end=day)
                if not logs:
                    record = TextRecord(record_id=f"missing:{day}:{person.person_id}",
                                        submitted=False, confirmed=True, **common)
                    records[record.record_id] = record
                    continue
                submitted.add((day, person.person_id))
                for log in logs:
                    units = units_for_log(snapshot, log.log_id)
                    if not team_scope:
                        units = [unit for unit in units
                                 if membership.department_id in unit.department_ids]
                    confirmed = bool(units) and all(
                        unit.status == UnitStatus.CONFIRMED for unit in units)
                    reviews: list[EvaluationText] = []
                    # 不带评价的材料（S04）只要求日志原文：日志当天归档，
                    # 评价却要等到次日人工确认，等它会让阶段汇总无谓地推迟一天。
                    if with_evaluations:
                        if not units:
                            missing.append(f"{day} 日志 {log.log_id} 生成错误：无可用评价")
                        for unit in units:
                            evaluator = people.get(unit.evaluator_id)
                            if unit.status not in {UnitStatus.WAITING_CONFIRMATION,
                                                   UnitStatus.CONFIRMED}:
                                missing.append(f"{day} 日志 {log.log_id} 生成错误：无可用评价")
                                continue
                            source, positive, improvement = effective_text(snapshot, unit)
                            if source == "FAILED":
                                missing.append(f"{day} 日志 {log.log_id} 缺少最终评价")
                                continue
                            if evaluator is None:
                                missing.append(f"{day} 日志 {log.log_id} 评价人缺失")
                                continue
                            review = EvaluationText(
                                evaluator_id=evaluator.person_id,
                                evaluator_role=evaluator.role,
                                evaluated_at=unit.confirmed_at or unit.evaluated_at,
                                positive=positive, improvement=improvement,
                                source=source,
                                department_ids=list(unit.department_ids),
                                department_name=departments[
                                    unit.department_ids[0]].name
                                if unit.department_ids and unit.department_ids[0] in departments
                                else "")
                            if source == "AI":
                                ai_state = snapshot.ai_evaluations.get(unit.log_id)
                                cleaned = _without_placeholders(
                                    [review], ai_state.fallback if ai_state else "")
                                if not cleaned:
                                    continue
                                review = cleaned[0]
                            reviews.append(review)
                    record = TextRecord(record_id=log.source_record_id or log.log_id,
                        confirmed=confirmed, progress=log.progress, difficulties=log.difficulties,
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

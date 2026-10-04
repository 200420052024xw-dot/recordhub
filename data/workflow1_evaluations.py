"""AI and form evaluation adapters using only existing Feishu columns."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from data.repositories import SHANGHAI, _datetime, _fields, _references, _scalar
from data.store import FileStateStore
from schema import DailySnapshot, FinalEvaluation, TableConfig, UnitStatus
from tool.feishu import BitableService


def evaluation_key(target_date: date, evaluator_id: str, log_id: str) -> str:
    return f"{target_date.isoformat()}:EVAL:{evaluator_id}:{log_id}"


class AiEvaluationRepository:
    def __init__(self, bitable: BitableService, config: TableConfig,
                 store: FileStateStore) -> None:
        self.bitable = bitable
        self.table = config.tables["evaluations"]
        self.store = store

    def publish(
        self, snapshot: DailySnapshot
    ) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
        """Idempotently write AI rows plus 未填写日志 marker rows.

        Reentrant: units already mapped (``record_id`` set), units still
        waiting on the LLM, and failed units (already in the issue ledger)
        are skipped. Rows missing a party's OpenID are skipped and reported
        through the returned issues instead of failing the whole day.
        """
        f = self.table.fields
        logs = {log.log_id: log for log in snapshot.logs}
        people = snapshot.organization.person_map()
        issues: dict[str, str] = {}
        existing = self.bitable.list_records(self.table.table_id)
        by_evaluation: dict[str, tuple[str, str]] = {}
        source_rows: dict[str, list[dict]] = defaultdict(list)
        for record in existing:
            fields = _fields(record)
            record_id = str(record.get("record_id", ""))
            evaluation_id = _scalar(fields.get(f["evaluation_id"]))
            if record_id and evaluation_id:
                by_evaluation[evaluation_id] = (record_id, evaluation_id)
            for reference in _references(fields.get(f["source_log"])):
                source_rows[reference].append(record)
        mapped: dict[str, tuple[str, str]] = {}
        # log_id is None for marker rows, which need no local mapping.
        creates: list[tuple[str | None, dict]] = []
        for state in snapshot.log_evaluations.values():
            if state.record_id:
                continue
            if not state.evaluator_id or state.status == UnitStatus.FAILED:
                continue
            if state.status not in {UnitStatus.WAITING_CONFIRMATION,
                                    UnitStatus.CONFIRMED}:
                continue
            log = logs[state.log_id]
            person = people[state.person_id]
            evaluator = people[state.evaluator_id]
            if not log.source_record_id:
                raise ValueError(f"日志 {log.log_id} 缺少原始飞书记录 ID")
            if not person.open_id or not evaluator.open_id:
                missing_id = state.person_id if not person.open_id else state.evaluator_id
                role = "提交人" if missing_id == state.person_id else "评价人"
                who = people[missing_id]
                issues[f"person:{missing_id}:missing-open-id"] = (
                    f"日志 {log.log_id} 的{role}「{who.name}」缺少 OpenID，"
                    "AI 评价行未写回，请补录后调用重评接口")
                continue
            key = evaluation_key(
                snapshot.target_date, state.evaluator_id, state.log_id)
            hit = by_evaluation.get(key)
            if hit is not None:
                mapped[state.log_id] = hit
                continue
            matches = []
            for record in source_rows.get(log.source_record_id, []):
                if person.open_id not in _references(
                        _fields(record).get(f["person_ref"])):
                    raise ValueError(f"AI 评价表中日志 {log.log_id} 已有关联但人员不匹配")
                matches.append(record)
            if len(matches) > 1:
                raise ValueError(f"AI 评价表中日志 {log.log_id} 存在重复记录")
            if matches:
                record = matches[0]
                record_id = str(record.get("record_id", ""))
                evaluation_id = _scalar(_fields(record).get(f["evaluation_id"]))
                if not record_id or not evaluation_id:
                    raise ValueError(f"AI 评价表中日志 {log.log_id} 缺少评价编号")
                mapped[state.log_id] = (record_id, evaluation_id)
                continue
            creates.append((state.log_id, {
                f["evaluation_id"]: key,
                f["person_ref"]: [{"id": person.open_id}],
                f["source_log"]: log.source_record_id,
                f["evaluated_at"]: int(datetime.now().timestamp() * 1000),
                f["positive_ai"]: state.positive_ai,
                f["improvement_ai"]: state.improvement_ai,
            }))
        target = snapshot.target_date.isoformat()
        for person_id in sorted(snapshot.submission_status):
            if snapshot.submission_status[person_id].submitted:
                continue
            person = people[person_id]
            key = f"{target}:MISSING:{person_id}"
            if key in by_evaluation:
                continue
            if not person.open_id:
                issues[f"person:{person_id}:missing-open-id"] = (
                    f"未交人员「{person.name}」缺少 OpenID，未填写日志标注行未写回")
                continue
            creates.append((None, {
                f["evaluation_id"]: key,
                f["person_ref"]: [{"id": person.open_id}],
                f["evaluated_at"]: int(datetime.now().timestamp() * 1000),
                f["positive_ai"]: "未填写日志",
                f["improvement_ai"]: "未填写日志",
            }))
        for offset in range(0, len(creates), 500):
            chunk = creates[offset:offset + 500]
            created = self.bitable.batch_create(
                self.table.table_id, [fields for _, fields in chunk])
            if len(created) != len(chunk):
                raise ValueError("飞书批量创建 AI 评价返回数量不一致")
            for (log_id, fields_sent), record in zip(chunk, created, strict=True):
                record_id = str(record.get("record_id", ""))
                if not record_id:
                    raise ValueError(f"日志 {log_id} 创建后没有记录 ID")
                evaluation_id = _scalar(_fields(record).get(f["evaluation_id"])) \
                    or fields_sent[f["evaluation_id"]]
                if log_id is not None:
                    mapped[log_id] = (record_id, evaluation_id)
        return mapped, issues


class HumanEvaluationRepository:
    def __init__(self, bitable: BitableService, config: TableConfig) -> None:
        self.bitable = bitable
        self.table = config.tables["human_evaluations"]

    def load_record_fields(self, record_id: str) -> dict:
        return _fields(self.bitable.get_record(self.table.table_id, record_id))

    def for_date(self, snapshot: DailySnapshot) -> list[dict]:
        records = self.bitable.list_records(self.table.table_id)
        field = self.table.fields.get("evaluated_at")
        if not field:
            return records
        end = datetime.combine(snapshot.target_date + timedelta(days=2),
                               datetime.min.time(), SHANGHAI)
        return [record for record in records
                if _fields(record).get(field)
                and snapshot.created_at <= _datetime(_fields(record)[field]) < end]

    def parse(self, snapshot: DailySnapshot,
              record: dict) -> tuple[str, FinalEvaluation] | None:
        f = self.table.fields
        values = _fields(record)
        evaluation_id = _scalar(values.get(f["evaluation_id"]))
        if not evaluation_id:
            return None
        matches = [state for state in snapshot.log_evaluations.values()
                   if state.evaluation_id == evaluation_id]
        if not matches:
            people = snapshot.organization.person_map()
            person_refs = set(_references(values.get(f["person_ref"])))
            authors = set(_references(values.get(f["submitted_by"])))
            matches = [state for state in snapshot.log_evaluations.values()
                       if state.evaluator_id
                       and people[state.person_id].open_id in person_refs
                       and people[state.evaluator_id].open_id in authors]
            if not matches:
                return None
        if len(matches) != 1:
            raise ValueError(f"人工评价 {evaluation_id} 无法唯一对应当日一条日志")
        state = matches[0]
        people = snapshot.organization.person_map()
        person = people[state.person_id]
        evaluator = people[state.evaluator_id]
        supplied = set(_references(values.get(f["person_ref"])))
        if not supplied.intersection({person.person_id, person.name,
                                      person.open_id, person.source_record_id}):
            raise ValueError(f"人工评价 {evaluation_id} 的 person_ref 与快照不符")
        evaluator_refs = set(_references(values.get(f["evaluator_ref"])))
        if evaluator_refs and not evaluator_refs.intersection(
                {evaluator.person_id, evaluator.name, evaluator.open_id,
                 evaluator.source_record_id}):
            raise ValueError(f"人工评价 {evaluation_id} 的 evaluator_ref 与快照不符")
        authors = set(_references(values.get(f["submitted_by"])))
        allowed = {evaluator.person_id, evaluator.name,
                   evaluator.open_id, evaluator.source_record_id}
        if not authors.intersection(allowed):
            raise ValueError(f"人工评价 {evaluation_id} 的填写人不是直属评价人")
        positive = _scalar(values.get(f["positive_final"])).strip()
        improvement = _scalar(values.get(f["improvement_final"])).strip()
        if not positive or not improvement:
            return None
        key = evaluation_key(snapshot.target_date, state.evaluator_id, state.log_id)
        return key, FinalEvaluation(
            person_id=state.person_id, log_id=state.log_id,
            evaluator_id=state.evaluator_id, positive=positive,
            improvement=improvement, source="HUMAN")

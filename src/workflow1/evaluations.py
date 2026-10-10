"""AI and form evaluation adapters using only existing Feishu columns."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta

from data.store import FileStateStore
from config.tables import human_evaluation_tables
from tool.bitable_fields import (
    SHANGHAI,
    record_fields,
    record_submission_time,
    references,
    scalar,
)
from schema import FinalEvaluation, TableConfig
from workflow1.models import DailySnapshot, UnitStatus
from tool.feishu import BitableService


def evaluation_key(target_date: date, evaluator_id: str, log_id: str) -> str:
    return f"{target_date.isoformat()}:EVAL:{evaluator_id}:{log_id}"


def review_day_datetime(target_date: date) -> datetime:
    """Midnight in Shanghai on the day the previous day's logs are reviewed."""
    review_day = target_date + timedelta(days=1)
    return datetime.combine(review_day, time.min, SHANGHAI)


def review_day_timestamp(target_date: date) -> int:
    return int(review_day_datetime(target_date).timestamp() * 1000)


class AiEvaluationRepository:
    def __init__(self, bitable: BitableService, config: TableConfig,
                 store: FileStateStore) -> None:
        self.bitable = bitable
        self.table = config.tables["evaluations"]
        self.store = store

    def backfill_source_logs(self, snapshot: DailySnapshot) -> int:
        """Correct existing AI rows identified by the saved snapshot."""
        field = self.table.fields["source_log"]
        records = {
            str(record.get("record_id", "")): record
            for record in self.bitable.list_records(self.table.table_id)
        }
        updates = []
        for log in snapshot.logs:
            state = snapshot.log_evaluations.get(log.log_id)
            if state is None or not state.record_id:
                continue
            record = records.get(state.record_id)
            if record is None:
                raise ValueError(f"AI 评价记录 {state.record_id} 不存在")
            if scalar(record_fields(record).get(field)) != log.content():
                updates.append({"record_id": state.record_id,
                                "fields": {field: log.content()}})
        for offset in range(0, len(updates), 500):
            self.bitable.batch_update(self.table.table_id, updates[offset:offset + 500])
        return len(updates)

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
        business_field = f.get("business_key")
        logs = {log.log_id: log for log in snapshot.logs}
        people = snapshot.organization.person_map()
        issues: dict[str, str] = {}
        existing = self.bitable.list_records(self.table.table_id)
        existing_by_id = {str(record.get("record_id", "")): record for record in existing}
        by_evaluation: dict[str, tuple[str, str]] = {}
        source_rows: dict[str, list[dict]] = defaultdict(list)
        evaluated_at = review_day_timestamp(snapshot.target_date)
        for record in existing:
            fields = record_fields(record)
            record_id = str(record.get("record_id", ""))
            evaluation_id = scalar(fields.get(f["evaluation_id"]))
            business_id = scalar(fields.get(business_field)) if business_field else evaluation_id
            if record_id and business_id:
                if business_field and not evaluation_id:
                    raise ValueError(f"AI 评价记录 {record_id} 缺少自动评价编号")
                by_evaluation[business_id] = (record_id, evaluation_id)
            source = scalar(fields.get(f["source_log"]))
            if source:
                source_rows[source].append(record)
        mapped: dict[str, tuple[str, str]] = {}
        # log_id is None for marker rows, which need no local mapping.
        creates: list[tuple[str | None, dict]] = []
        for state in snapshot.log_evaluations.values():
            if state.record_id:
                person = people[state.person_id]
                record = existing_by_id.get(state.record_id)
                if (record and person.role == "基层学生" and f.get("person_name")
                        and scalar(record_fields(record).get(f["person_name"])).strip()
                        != person.name):
                    self.bitable.update_record(self.table.table_id, state.record_id,
                                               {f["person_name"]: person.name})
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
            if (person.role != "基层学生" and not person.open_id) or not evaluator.open_id:
                missing_id = state.person_id if not person.open_id and person.role != "基层学生" else state.evaluator_id
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
                current = record_fields(existing_by_id[hit[0]])
                updates = {}
                if scalar(current.get(f["source_log"])) != log.content():
                    updates[f["source_log"]] = log.content()
                if person.role == "基层学生" and f.get("person_name") and (
                        scalar(current.get(f["person_name"])).strip() != person.name):
                    updates[f["person_name"]] = person.name
                if updates:
                    self.bitable.update_record(self.table.table_id, hit[0], updates)
                mapped[state.log_id] = hit
                continue
            matches = []
            candidates = source_rows.get(log.source_record_id, []) + source_rows.get(log.content(), [])
            for record in candidates:
                prior = record_fields(record)
                same_person = (scalar(prior.get(f["person_name"])).strip() == person.name
                               or person.open_id in references(prior.get(f["person_ref"]))
                               if person.role == "基层学生" and f.get("person_name")
                               else person.open_id in references(prior.get(f["person_ref"])))
                if not same_person:
                    raise ValueError(f"AI 评价表中日志 {log.log_id} 已有关联但人员不匹配")
                matches.append(record)
            if len(matches) > 1:
                raise ValueError(f"AI 评价表中日志 {log.log_id} 存在重复记录")
            if matches:
                record = matches[0]
                record_id = str(record.get("record_id", ""))
                evaluation_id = scalar(record_fields(record).get(f["evaluation_id"]))
                if not record_id or not evaluation_id:
                    raise ValueError(f"AI 评价表中日志 {log.log_id} 缺少评价编号")
                current = record_fields(record)
                updates = {}
                if scalar(current.get(f["source_log"])) != log.content():
                    updates[f["source_log"]] = log.content()
                if person.role == "基层学生" and f.get("person_name") and (
                        scalar(current.get(f["person_name"])).strip() != person.name):
                    updates[f["person_name"]] = person.name
                if updates:
                    self.bitable.update_record(self.table.table_id, record_id, updates)
                mapped[state.log_id] = (record_id, evaluation_id)
                continue
            identity = ({f["person_name"]: person.name}
                        if person.role == "基层学生" and f.get("person_name")
                        else {f["person_ref"]: [{"id": person.open_id}]})
            creates.append((state.log_id, {
                business_field or f["evaluation_id"]: key,
                **identity,
                f["source_log"]: log.content(),
                f["evaluated_at"]: evaluated_at,
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
                hit = by_evaluation[key]
                if person.role == "基层学生" and f.get("person_name") and (
                        scalar(record_fields(existing_by_id[hit[0]]).get(f["person_name"])).strip()
                        != person.name):
                    self.bitable.update_record(self.table.table_id, hit[0],
                                               {f["person_name"]: person.name})
                continue
            if person.role != "基层学生" and not person.open_id:
                issues[f"person:{person_id}:missing-open-id"] = (
                    f"未交人员「{person.name}」缺少 OpenID，未填写日志标注行未写回")
                continue
            identity = ({f["person_name"]: person.name}
                        if person.role == "基层学生" and f.get("person_name")
                        else {f["person_ref"]: [{"id": person.open_id}]})
            creates.append((None, {
                business_field or f["evaluation_id"]: key,
                **identity,
                f["evaluated_at"]: evaluated_at,
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
                evaluation_id = scalar(record_fields(record).get(f["evaluation_id"]))
                if business_field and not evaluation_id:
                    evaluation_id = scalar(record_fields(self.bitable.get_record(
                        self.table.table_id, record_id)).get(f["evaluation_id"]))
                if not evaluation_id:
                    if business_field:
                        raise ValueError(f"AI 评价记录 {record_id} 缺少自动评价编号")
                    evaluation_id = scalar(fields_sent.get(f["evaluation_id"]))
                if log_id is not None:
                    mapped[log_id] = (record_id, evaluation_id)
        return mapped, issues


class HumanEvaluationRepository:
    def __init__(self, bitable: BitableService, config: TableConfig,
                 cutoff_at: str = "19:00") -> None:
        self.bitable = bitable
        self.table = config.tables["human_evaluations"]
        self.tables = human_evaluation_tables(config)
        self.tables_by_id = {table.table_id: table for table in self.tables.values()}
        self.cutoff_at = cutoff_at

    def load_record(self, record_id: str, *, table_id: str | None = None) -> dict:
        table_id = table_id or self.table.table_id
        if table_id not in self.tables_by_id:
            raise ValueError("人工评价表 ID 不匹配")
        return {**self.bitable.get_record(table_id, record_id), "_table_id": table_id}

    def _record_table(self, record: dict):
        table_id = record.get("_table_id", self.table.table_id)
        if table_id not in self.tables_by_id:
            raise ValueError("人工评价表 ID 不匹配")
        return self.tables_by_id[table_id]

    def _within_window(self, snapshot: DailySnapshot, record: dict) -> bool:
        field = self._record_table(record).fields.get("evaluated_at")
        if not field:
            return True
        submitted = record_submission_time(record, field)
        if submitted is None:
            return False
        start = datetime.combine(snapshot.target_date + timedelta(days=1),
                                 time.min, SHANGHAI)
        if self.cutoff_at:
            hour, minute = map(int, self.cutoff_at.split(":"))
            end = datetime.combine(snapshot.target_date + timedelta(days=1),
                                   time(hour, minute), SHANGHAI)
        else:
            end = datetime.combine(snapshot.target_date + timedelta(days=2),
                                   time.min, SHANGHAI)
        return start <= submitted < end

    def for_date(self, snapshot: DailySnapshot) -> list[dict]:
        records = [{**record, "_table_id": table.table_id}
                   for table in self.tables.values()
                   for record in self.bitable.list_records(table.table_id)]
        return [record for record in records if self._within_window(snapshot, record)]

    def parse(self, snapshot: DailySnapshot,
              record: dict) -> tuple[str, FinalEvaluation] | None:
        f = self._record_table(record).fields
        if not self._within_window(snapshot, record):
            return None
        values = record_fields(record)
        evaluation_id = scalar(values.get(f["evaluation_id"]))
        if not evaluation_id:
            return None
        matches = [state for state in snapshot.log_evaluations.values()
                   if state.evaluation_id == evaluation_id]
        if not matches:
            people = snapshot.organization.person_map()
            authors = set(references(values.get(f["submitted_by"])))
            matches = [state for state in snapshot.log_evaluations.values()
                       if state.evaluator_id
                       and self._matches_person(values, f, people[state.person_id])
                       and people[state.evaluator_id].open_id in authors]
            if not matches:
                return None
        if len(matches) != 1:
            raise ValueError(f"人工评价 {evaluation_id} 无法唯一对应当日一条日志")
        state = matches[0]
        people = snapshot.organization.person_map()
        person = people[state.person_id]
        evaluator = people[state.evaluator_id]
        if not self._matches_person(values, f, person):
            raise ValueError(f"人工评价 {evaluation_id} 的被审核人与快照不符")
        evaluator_refs = set(references(values.get(f.get("evaluator_ref", ""))))
        if evaluator_refs and not evaluator_refs.intersection(
                {evaluator.person_id, evaluator.name, evaluator.open_id,
                 evaluator.source_record_id}):
            raise ValueError(f"人工评价 {evaluation_id} 的 evaluator_ref 与快照不符")
        authors = set(references(values.get(f["submitted_by"])))
        allowed = {evaluator.person_id, evaluator.name,
                   evaluator.open_id, evaluator.source_record_id}
        if not authors.intersection(allowed):
            raise ValueError(f"人工评价 {evaluation_id} 的填写人不是直属评价人")
        positive_flag = scalar(values.get(f["positive_confirmed"])).strip() if f.get("positive_confirmed") else ""
        improvement_flag = scalar(values.get(f["improvement_confirmed"])).strip() if f.get("improvement_confirmed") else ""
        if f.get("positive_confirmed") and f.get("improvement_confirmed"):
            if not positive_flag or not improvement_flag:
                return None
            allowed = {"确认无误", "需修改"}
            if positive_flag not in allowed or improvement_flag not in allowed:
                raise ValueError(f"人工评价 {evaluation_id} 的确认选项无效")
            positive = (state.positive_ai or "") if positive_flag == "确认无误" else scalar(values.get(f["positive_final"])).strip()
            improvement = (state.improvement_ai or "") if improvement_flag == "确认无误" else scalar(values.get(f["improvement_final"])).strip()
        else:
            positive = scalar(values.get(f["positive_final"])).strip()
            improvement = scalar(values.get(f["improvement_final"])).strip()
        if not positive or not improvement:
            return None
        key = evaluation_key(snapshot.target_date, state.evaluator_id, state.log_id)
        return key, FinalEvaluation(
            person_id=state.person_id, log_id=state.log_id,
            evaluator_id=state.evaluator_id, positive=positive,
            improvement=improvement, source="HUMAN")

    @staticmethod
    def _matches_person(values: dict, fields: dict, person) -> bool:
        if person.role == "基层学生" and fields.get("basic_name"):
            name = scalar(values.get(fields["basic_name"])).strip()
            return bool(name and name == person.name
                        and not references(values.get(fields.get("backbone_ref", ""))))
        ref_field = fields.get("backbone_ref") or fields.get("person_ref")
        supplied = set(references(values.get(ref_field)))
        if fields.get("basic_name") and scalar(values.get(fields["basic_name"])).strip():
            return False
        return bool(supplied.intersection({person.person_id, person.name,
                                           person.open_id, person.source_record_id}))

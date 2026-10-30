"""对话入口（WorkBuddy）批改口径：取待批改清单、逐条收下批改结果。

只负责「收下」：把老师的批改结果暂存到本地，返回「已收到」。真正定稿由后端夜间
与飞书表单合并后统一处理，所以这里不出现「已确认 / 已定稿」。

待批改的来源：
- 日志评价：审核日（今天）看的是**昨天**日志的 AI 评价快照，取「等人工确认」的。
- 月报 / 阶段工作总结：读 Workflow2 的草稿表（待实现）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from data.repositories import OrganizationCache
from data.store import FileStateStore
from schema import TableConfig
from schema.domain import Organization, Person
from service.workbuddy_query.cursor import decode_cursor, encode_cursor
from service.workbuddy_query.models import QueryError
from service.workbuddy_query.permissions import ROLE_MINISTER, member_roles
from tool.bitable_fields import (alias_index, field_datetime, record_fields,
                                 record_submission_time, references,
                                 resolve_reference, scalar)
from tool.feishu import BitableService
from workflow1.models import DailySnapshot, LogEvaluationState, UnitStatus
from workflow1.units import ai_for, evaluation_table_for, unit_key, units_for_evaluator

from service.workbuddy_review.models import (
    CLOSED,
    EMPTY_EDITED,
    ITEM_NOT_FOUND,
    KIND_LOG,
    KIND_MONTHLY,
    KIND_STAGE,
    KINDS,
    NOT_YOURS,
    OK,
    REVIEW_FAILED,
    PendingRequest,
    ReviewError,
    ReviewSubmitRequest,
)
from service.workbuddy_review.settings import ReviewSettings
from service.workbuddy_review.store import ReviewInbox

logger = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
REVIEW_PAGE_SIZE = 50

# 产出类型 → 对外条目 kind / 中文名。
_ITEM_KIND = {KIND_LOG: "log_evaluation", KIND_MONTHLY: "monthly", KIND_STAGE: "stage_summary"}
_KIND_LABEL = {KIND_LOG: "日志评价", KIND_MONTHLY: "月报", KIND_STAGE: "阶段工作总结"}

# 报告类：草稿表 / 确认表 / 草稿内容字段（按 tables.toml 的字段键）。
_REPORT_TABLES = {
    KIND_STAGE: ("stage_analysis", "stage_confirmation", ("progress",)),
    KIND_MONTHLY: ("monthly_department_analysis", "monthly_department_confirmation",
                   ("personnel_s05", "ideas_s06", "meeting_s07")),
}
# 报告类：确认表里表示「已确认」的列（任一非空即算确认过）。
_REPORT_DECISION_KEYS = {
    KIND_STAGE: ("decision",),
    KIND_MONTHLY: ("personnel_decision", "ideas_decision", "meeting_decision"),
}
# 月报草稿字段 → 确认表的（是否确认列, 修改内容列）。
_MONTHLY_CONFIRM = {
    "personnel_s05": ("personnel_decision", "personnel_edited"),
    "ideas_s06": ("ideas_decision", "ideas_edited"),
    "meeting_s07": ("meeting_decision", "meeting_edited"),
}


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _aliases(organization: Organization):
    """部长引用字段可能写成 person_id / 姓名 / open_id / 源记录号，统一成别名表。"""
    return alias_index(
        (person.person_id,
         (person.person_id, person.name, person.open_id or "",
          person.source_record_id or ""))
        for person in organization.persons)


def _person_ref(person: Person):
    """人员字段：有 open_id 用引用，否则退回姓名（与既有解析的容错一致）。"""
    return [{"id": person.open_id}] if person.open_id else person.name


def _person_ids(person: Person) -> set[str]:
    """一个人在各种引用里可能出现的写法，用于把表里的引用认回人。"""
    return {value for value in (person.person_id, person.name, person.open_id or "",
                                person.source_record_id or "") if value}


def _report_sections(values: dict, fields: dict, content_keys) -> list[dict]:
    sections = []
    for key in content_keys:
        column = fields.get(key)
        if not column:
            continue
        text = str(scalar(values.get(column))).strip()
        if text:
            sections.append({"label": column, "text": text})
    return sections


class ReviewService:
    def __init__(self, *, organization_cache: OrganizationCache,
                 snapshot_store: FileStateStore, inbox: ReviewInbox,
                 settings: ReviewSettings, cursor_secret: str,
                 bitable: BitableService | None = None,
                 tables: TableConfig | None = None,
                 clock: Callable[[], datetime] | None = None,
                 page_size: int = REVIEW_PAGE_SIZE) -> None:
        self.organization_cache = organization_cache
        self.snapshot_store = snapshot_store
        self.inbox = inbox
        self.settings = settings
        self.cursor_secret = cursor_secret
        self.bitable = bitable
        self.tables = tables
        self._clock = clock or (lambda: datetime.now(SHANGHAI))
        self.page_size = page_size

    # ------------------------------------------------------------ 待批改清单

    def pending(self, request: PendingRequest) -> dict:
        try:
            return self._pending(request)
        except ReviewError as exc:
            return exc.payload()

    def _pending(self, request: PendingRequest) -> dict:
        person = self._reviewer(request.person_id)
        now = self._clock()
        if self._is_closed(now):
            raise ReviewError(CLOSED, deadline_at=self._deadline(now).isoformat())
        today = now.date()
        if request.kind == KIND_LOG:
            items, missing = self._pending_logs(person, today)
        else:
            items, missing = self._pending_reports(person, request.kind, today)
        page, next_cursor = self._paginate(items, request.cursor)
        deadlines = []
        if items:
            deadlines.append({
                "kind": _ITEM_KIND[request.kind],
                "label": _KIND_LABEL[request.kind],
                "count": len(items),
                "deadline_at": self._deadline(now).isoformat(),
            })
        return {"code": OK, "deadlines": deadlines, "missing": missing,
                "items": page, "next_cursor": next_cursor}

    # ------------------------------------------------------------- 逐条提交

    def submit(self, request: ReviewSubmitRequest) -> dict:
        try:
            return self._submit(request)
        except ReviewError as exc:
            return exc.payload()

    def _submit(self, request: ReviewSubmitRequest) -> dict:
        person = self._reviewer(request.person_id)
        now = self._clock()
        if self._is_closed(now):
            raise ReviewError(CLOSED, deadline_at=self._deadline(now).isoformat())
        today = now.date()
        kind, item = self._locate(person, request.item_id, today)
        edited = None
        if request.decision == "edit":
            if request.edited is None or request.edited.is_empty():
                raise ReviewError(EMPTY_EDITED, item_id=request.item_id)
            edited = request.edited.normalized()
        payload: dict = {
            "decision": request.decision,
            "reviewer_id": person.person_id,
            "received_at": now.isoformat(),
        }
        if edited is not None:
            payload["edited"] = edited
        snapshot = self._ai_snapshot(item)
        if snapshot is not None:
            payload["ai_snapshot"] = snapshot
        self.inbox.record(kind, person.name, reviewer_id=person.person_id,
                          target_date=today, item_id=request.item_id, payload=payload)
        logger.info("review_submitted kind=%s reviewer_id=%s item_id=%s decision=%s",
                    kind, person.person_id, request.item_id, request.decision)
        return {"code": OK, "item_id": request.item_id,
                "decision": request.decision, "received_at": payload["received_at"]}

    # --------------------------------------------------------- 日志评价队列

    def _pending_logs(self, person: Person, today: date) -> tuple[list[dict], list[dict]]:
        """审核日看昨天日志的 AI 评价快照，按评价单元取「等人工确认」的。"""
        snapshot = self.snapshot_store.load_snapshot(today - timedelta(days=1))
        if snapshot is None:
            return [], []
        people = snapshot.organization.person_map()
        departments = snapshot.organization.department_map()
        items: list[dict] = []
        for unit in units_for_evaluator(snapshot, person.person_id):
            if unit.status != UnitStatus.WAITING_CONFIRMATION:
                continue
            log = next((item for item in snapshot.logs
                        if item.log_id == unit.log_id), None)
            if log is None:
                continue
            items.append(self._log_item(log, unit, ai_for(snapshot, unit.log_id),
                                        people, departments, snapshot.target_date))
        items.sort(key=lambda item: item["item_id"])
        return items, self._missing(snapshot, people, person)

    @staticmethod
    def _log_item(log, unit: LogEvaluationState, ai_state, people: dict,
                  departments: dict, target_date: date) -> dict:
        person = people.get(unit.person_id)
        evaluator = people.get(unit.evaluator_id)
        failed = unit.fallback == "failed" or ai_state is None or ai_state.fallback == "failed"
        department_names = []
        for department_id in unit.department_ids:
            department = departments.get(department_id)
            department_names.append(department.name if department else department_id)
        item: dict = {
            "item_id": f"{log.log_id}:{unit.evaluator_id}",
            "kind": "log_evaluation",
            "date": target_date.isoformat(),
            "name": person.name if person else log.person_id,
            "log": {"progress": log.progress, "difficulties": log.difficulties,
                    "reflection": log.reflection, "other": log.other},
            "ai_status": "failed" if failed else "ok",
            "department_id": unit.department_ids[0] if unit.department_ids else "",
            "department_name": department_names[0] if department_names else "",
            "departments": department_names,
            "evaluator_role": evaluator.role if evaluator else "",
            "subject_role": person.role if person else "",
        }
        if not failed and ai_state is not None:
            # 生成失败的条目没有 AI 原文可念，让老师现场说。
            item["positive"] = ai_state.positive_ai or ""
            item["improvement"] = ai_state.improvement_ai or ""
        return item

    @staticmethod
    def _missing(snapshot: DailySnapshot, people: dict,
                 person: Person) -> list[dict]:
        """审核人下属里，日志日当天没交日志的人（只用于摘要）。"""
        submitted = {log.person_id for log in snapshot.logs}
        missing = []
        for report in snapshot.organization.subordinates().get(person.person_id, []):
            if report.person_id in submitted:
                continue
            missing.append({"date": snapshot.target_date.isoformat(),
                            "name": report.name})
        missing.sort(key=lambda item: item["name"])
        return missing

    # ------------------------------------------------------------- 报告队列

    def _pending_reports(self, person: Person, kind: str,
                         today: date) -> tuple[list[dict], list[dict]]:
        """报告类只由部长确认：读本部门草稿表里还没确认的那一份。"""
        if ROLE_MINISTER not in member_roles(person):
            return [], []
        items = [item for item, owner in self._report_rows(kind)
                 if owner == person.person_id]
        items.sort(key=lambda item: (item["date"], item["name"]))
        return items, []

    def _report_rows(self, kind: str) -> list[tuple[dict, str]]:
        """每个部长**最近一轮**且还没确认的草稿：(条目, 部长 person_id)。

        同一部门可能攒了几轮没确认的草稿，只给最近一轮，别让老师看到旧的。
        """
        if self.tables is None or self.bitable is None or kind not in _REPORT_TABLES:
            return []
        analysis_name, confirmation_name, content_keys = _REPORT_TABLES[kind]
        table = self.tables.tables.get(analysis_name)
        if table is None or not table.table_id.strip():
            return []
        fields = table.fields
        organization = self.organization_cache.get()
        aliases = _aliases(organization)
        departments = organization.department_map()
        people = organization.person_map()
        confirmed = self._confirmed_task_ids(kind, confirmation_name)
        latest: dict[str, tuple[date, dict, str]] = {}
        for row in self.bitable.list_records(table.table_id):
            values = record_fields(row)
            owner = resolve_reference(values.get(fields.get("minister_ref", "")), aliases)
            if not owner:
                continue
            task_id = str(scalar(values.get(fields.get("task_id", "")))).strip()
            if not task_id:
                continue
            day = self._day(values.get(fields.get("analysis_date", "")))
            sections = _report_sections(values, fields, content_keys)
            if day is None or not sections:
                continue
            minister = people.get(owner)
            department_id = (task_id.rsplit(":", 1)[-1]
                             if ":" in task_id else
                             (minister.department_id if minister else None))
            department = departments.get(department_id or "")
            name = (department.name if department
                    else (minister.name if minister else owner))
            item = {"item_id": task_id, "kind": _ITEM_KIND[kind],
                    "date": day.isoformat(), "name": name, "sections": sections}
            # 任务编号形如 {run_id}:{department_id}，按部门保留最近一轮。
            key = task_id.rsplit(":", 1)[-1] or owner
            current = latest.get(key)
            if current is None or day > current[0]:
                latest[key] = (day, item, owner)
        # 只出最近一轮；最近一轮已确认就整轮不出。
        return [(item, owner) for _, item, owner in latest.values()
                if item["item_id"] not in confirmed]

    def _confirmed_task_ids(self, kind: str, confirmation_name: str) -> set[str]:
        table = self.tables.tables.get(confirmation_name) if self.tables else None
        if table is None or not table.table_id.strip() or self.bitable is None:
            return set()
        fields = table.fields
        keys = _REPORT_DECISION_KEYS.get(kind, ())
        confirmed: set[str] = set()
        for row in self.bitable.list_records(table.table_id):
            values = record_fields(row)
            task_id = str(scalar(values.get(fields.get("task_id", "")))).strip()
            if task_id and any(str(scalar(values.get(fields.get(key, "")))).strip()
                               for key in keys):
                confirmed.add(task_id)
        return confirmed

    @staticmethod
    def _day(raw) -> date | None:
        if not raw:
            return None
        try:
            return field_datetime(raw).astimezone(SHANGHAI).date()
        except (ValueError, TypeError):
            return None

    # --------------------------------------------------------- 合并与写回

    def merge(self, log_date: date) -> dict:
        """把暂存的批改结果合并进飞书表，供既有定稿流程读取。

        21:40 接口截止后跑一次；按 ``received_at`` 后覆盖前，重复执行安全。
        """
        review_date = log_date + timedelta(days=1)
        written = {"logs": self._merge_logs(log_date),
                   "reports": self._merge_reports(review_date)}
        logger.info("review_merge_done log_date=%s written=%s", log_date, written)
        return written

    def _merge_logs(self, log_date: date) -> int:
        if self.tables is None or self.bitable is None:
            return 0
        snapshot = self.snapshot_store.load_snapshot(log_date)
        if snapshot is None:
            return 0
        people = snapshot.organization.person_map()
        logs = {log.log_id: log for log in snapshot.logs}
        review_day = log_date + timedelta(days=1)
        written = 0
        for document in self.inbox.documents(KIND_LOG):
            reviewer_id = str(document.get("reviewer_id") or "")
            for item_id, payload in (document.get("items") or {}).items():
                log_id, separator, evaluator_id = item_id.rpartition(":")
                if separator and log_id:
                    unit = snapshot.evaluation_units.get(
                        unit_key(log_id, evaluator_id))
                else:
                    # legacy item_id = log_id；按暂存文件里的审核人定位单元。
                    unit = next((candidate for candidate in snapshot.evaluation_units.values()
                                 if candidate.log_id == item_id
                                 and candidate.evaluator_id == reviewer_id), None)
                if unit is None or unit.log_id not in logs:
                    continue
                person = people.get(unit.person_id)
                evaluator = people.get(unit.evaluator_id)
                if person is None or evaluator is None:
                    continue
                table_name = evaluation_table_for(unit, snapshot.organization)
                fields = self._log_row(table_name, person, evaluator, payload)
                if self._upsert_log_row(table_name, person, evaluator, review_day,
                                        fields, payload):
                    written += 1
            self.inbox.discard(KIND_LOG, document.get("reviewer_name", ""))
        return written

    def _log_row(self, table_name: str, person: Person, evaluator: Person,
                 payload: dict) -> dict:
        """只写人工能填的列；评价编号是自动编号、日志原文是查找引用，交给飞书。"""
        fields = self.tables.tables[table_name].fields
        edited = payload.get("edited") or {}
        ai = payload.get("ai_snapshot") or {}
        accept = payload.get("decision") == "accept"
        positive = (ai.get("positive") or "") if accept else (
            edited.get("positive") or ai.get("positive") or "")
        improvement = (ai.get("improvement") or "") if accept else (
            edited.get("improvement") or ai.get("improvement") or "")
        row = {
            fields["positive_final"]: positive,
            fields["improvement_final"]: improvement,
            fields["positive_confirmed"]: ("确认无误" if accept or not edited.get("positive")
                                           else "需修改"),
            fields["improvement_confirmed"]: ("确认无误" if accept or not edited.get("improvement")
                                              else "需修改"),
            fields["submitted_by"]: _person_ref(evaluator),
            fields["evaluated_at"]: self._millis(),
        }
        if table_name == "human_evaluations":
            row[fields["backbone_ref"]] = _person_ref(person)
        elif fields.get("basic_name"):
            row[fields["basic_name"]] = person.name
        return row

    def _merge_reports(self, review_date: date) -> int:
        if self.tables is None or self.bitable is None:
            return 0
        written = 0
        for kind in (KIND_STAGE, KIND_MONTHLY):
            analysis_name, confirmation_name, _ = _REPORT_TABLES[kind]
            table = self.tables.tables.get(confirmation_name)
            if table is None or not table.table_id.strip():
                continue
            analysis = self.tables.tables.get(analysis_name)
            key_to_label = dict(analysis.fields.items()) if analysis else {}
            cutoff = self._cutoff_on(review_date)
            for document in self.inbox.documents(kind):
                for task_id, payload in (document.get("items") or {}).items():
                    fields = self._report_row(kind, table.fields, key_to_label,
                                              task_id, payload)
                    if self._upsert_row(confirmation_name, "task_id", task_id,
                                        fields, payload, cutoff):
                        written += 1
                self.inbox.discard(kind, document.get("reviewer_name", ""))
        return written

    @staticmethod
    def _report_row(kind: str, fields: dict, key_to_label: dict,
                    task_id: str, payload: dict) -> dict:
        accept = payload.get("decision") == "accept"
        sections = {section["label"]: section.get("text", "")
                    for section in (payload.get("edited") or {}).get("sections", [])}
        row = {fields["task_id"]: task_id}
        if kind == KIND_STAGE:
            text = "\n".join(value for value in sections.values() if value)
            row[fields["decision"]] = "确认无误" if accept or not text else "需修改"
            if text:
                row[fields["edited"]] = text
            return row
        for key, (decision_key, edited_key) in _MONTHLY_CONFIRM.items():
            text = sections.get(key_to_label.get(key, "")) if not accept else ""
            if text:
                row[fields[decision_key]] = "需修改"
                row[fields[edited_key]] = text
            else:
                row[fields[decision_key]] = "确认无误"
        return row

    def _upsert_log_row(self, table_name: str, person: Person, evaluator: Person,
                        review_day: date, fields: dict, payload: dict) -> bool:
        """人工评价表：评价编号是飞书自动编号、日志原文是查找引用，都不能写。
        改按「被审核人 + 评价时间当天」认行；飞书那边更晚就保留，否则覆盖。"""
        table = self.tables.tables[table_name]
        received_at = _parse_time(payload.get("received_at"))
        cutoff = self._cutoff_on(review_day)
        for row in self.bitable.list_records(table.table_id):
            if not self._same_log_row(row, table_name, person, evaluator, review_day):
                continue
            submitted = record_submission_time(row, table.fields.get("evaluated_at", ""))
            # 接口截止（21:40）之后的飞书行忽略，不让它压过对话里的批改。
            if (submitted is not None and received_at is not None
                    and submitted <= cutoff and submitted >= received_at):
                return False
            self.bitable.update_record(table.table_id, str(row["record_id"]), fields)
            return True
        self.bitable.create_record(table.table_id, fields)
        return True

    def _same_log_row(self, row: dict, table_name: str, person: Person,
                      evaluator: Person, review_day: date) -> bool:
        fields = self.tables.tables[table_name].fields
        values = record_fields(row)
        if table_name == "human_evaluations":
            if not set(references(values.get(fields.get("backbone_ref", "")))).intersection(
                    _person_ids(person)):
                return False
        elif scalar(values.get(fields.get("basic_name", ""))).strip() != person.name.strip():
            return False
        authors = set(references(values.get(fields.get("submitted_by", ""))))
        if authors and not authors.intersection(_person_ids(evaluator)):
            return False
        when = values.get(fields.get("evaluated_at", ""))
        if when:
            try:
                if field_datetime(when).astimezone(SHANGHAI).date() != review_day:
                    return False
            except (TypeError, ValueError):
                return False
        return True

    def _upsert_row(self, table_name: str, key_field: str, key_value: str,
                    fields: dict, payload: dict, cutoff: datetime) -> bool:
        """按业务键找现有行：截止前飞书那边更晚就保留，否则覆盖；没有就新建。"""
        table = self.tables.tables[table_name]
        column = table.fields[key_field]
        received_at = _parse_time(payload.get("received_at"))
        for row in self.bitable.list_records(table.table_id):
            if scalar(record_fields(row).get(column)) != key_value:
                continue
            submitted = record_submission_time(row, table.fields.get("evaluated_at", ""))
            if (submitted is not None and received_at is not None
                    and submitted <= cutoff and submitted >= received_at):
                return False
            self.bitable.update_record(table.table_id, str(row["record_id"]), fields)
            return True
        self.bitable.create_record(table.table_id, fields)
        return True

    def _millis(self) -> int:
        return int(self._clock().timestamp() * 1000)

    # ------------------------------------------------------------- 公共底座

    def _reviewer(self, person_id: str) -> Person:
        person = self.organization_cache.get().person_map().get(person_id)
        if person is None or not person.active:
            raise ReviewError(NOT_YOURS, person_id=person_id)
        return person

    def _locate(self, person: Person, item_id: str,
                today: date) -> tuple[str, dict]:
        """找到这条待批改内容：不存在报 ITEM_NOT_FOUND，存在但不归这个人报 NOT_YOURS。"""
        for kind in KINDS:
            found = (self._lookup_log(item_id, today) if kind == KIND_LOG
                     else self._lookup_report(kind, item_id, today))
            if found is None:
                continue
            item, reviewer_id = found
            if reviewer_id != person.person_id:
                raise ReviewError(NOT_YOURS, item_id=item_id)
            return kind, item
        raise ReviewError(ITEM_NOT_FOUND, item_id=item_id)

    def _lookup_log(self, item_id: str, today: date) -> tuple[dict, str] | None:
        snapshot = self.snapshot_store.load_snapshot(today - timedelta(days=1))
        if snapshot is None:
            return None
        log_id, separator, evaluator_id = item_id.rpartition(":")
        if separator and log_id:
            unit = snapshot.evaluation_units.get(unit_key(log_id, evaluator_id))
        else:
            # legacy: item_id is the raw log id; 单评价人时代直接按 log_id 找。
            candidates = [unit for unit in snapshot.evaluation_units.values()
                          if unit.log_id == item_id]
            unit = candidates[0] if len(candidates) == 1 else None
        if unit is None or unit.status != UnitStatus.WAITING_CONFIRMATION:
            return None
        log = next((item for item in snapshot.logs if item.log_id == unit.log_id), None)
        if log is None:
            return None
        people = snapshot.organization.person_map()
        departments = snapshot.organization.department_map()
        item = self._log_item(log, unit, ai_for(snapshot, unit.log_id),
                              people, departments, snapshot.target_date)
        return item, unit.evaluator_id

    def _lookup_report(self, kind: str, item_id: str, today: date) -> tuple[dict, str] | None:
        for item, owner in self._report_rows(kind):
            if item["item_id"] == item_id:
                return item, owner
        return None

    @staticmethod
    def _ai_snapshot(item: dict) -> dict | None:
        if item["kind"] == "log_evaluation":
            if item.get("ai_status") == "failed":
                return None
            return {"positive": item.get("positive", ""),
                    "improvement": item.get("improvement", "")}
        return {"sections": item.get("sections", [])}

    def _paginate(self, items: list[dict],
                  cursor: str | None) -> tuple[list[dict], str | None]:
        offset = self._offset(cursor)
        page = items[offset:offset + self.page_size]
        next_offset = offset + self.page_size
        next_cursor = (encode_cursor(next_offset, self.cursor_secret)
                       if next_offset < len(items) else None)
        return page, next_cursor

    def _offset(self, cursor: str | None) -> int:
        if not cursor:
            return 0
        try:
            return decode_cursor(cursor, self.cursor_secret)
        except QueryError as exc:
            raise ReviewError(REVIEW_FAILED) from exc

    def _cutoff_on(self, day: date) -> datetime:
        """某天的接口截止时刻（默认 21:40）。合并只认这一刻之前的飞书行。"""
        hour, minute = self.settings.close_parts()
        return datetime.combine(day, time(hour, minute), SHANGHAI)

    def _deadline(self, now: datetime) -> datetime:
        return self._cutoff_on(now.date())

    def _is_closed(self, now: datetime) -> bool:
        return now >= self._deadline(now)

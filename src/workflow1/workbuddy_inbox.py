"""对话入口(WorkBuddy)提交工作日志:只增缓存,次日随工作流写回飞书。

学生提交的四栏日志先落成一份只增不改的 JSON 缓存;次日 Workflow1 启动时由
``LogSubmitService.flush`` 统一写回日志表,再走既有的 AI 评价链路。

写回失败的取舍:抛错让当日工作流 FAILED(可 resume),绝不带着缺失的日志建
快照——那会把已交的学生写成「未填写日志」。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from data.repositories import OrganizationCache
from data.store import FileStateStore
from schema import Organization, Person, TableConfig, WorkLog
from tool.bitable_fields import SHANGHAI, field_datetime, record_fields, scalar
from tool.feishu import BitableService
from workflow1.models import WorkBuddySubmission, WorkBuddySubmitRequest
from workflow1.settings import LogSubmitSettings

logger = logging.getLogger(__name__)

# 本期只做基层/骨干的按天日志;部长周日志不在范围内。
SUPPORTED_ROLES = frozenset({"基层学生", "骨干学生"})

OK = "OK"
CLOSED = "CLOSED"
EMPTY_CONTENT = "EMPTY_CONTENT"
NO_PERSON = "NO_PERSON"
PERSON_ID_UNKNOWN = "PERSON_ID_UNKNOWN"
ROLE_NOT_SUPPORTED = "ROLE_NOT_SUPPORTED"

_BUSINESS_KEY_PREFIX = "workbuddy:log"
_BATCH_SIZE = 500


def _millis(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _field_millis(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return _millis(field_datetime(value))
    except (TypeError, ValueError):
        return None


def _day_bounds_ms(day: date) -> tuple[int, int]:
    start = datetime.combine(day, time.min, SHANGHAI)
    return _millis(start), _millis(start + timedelta(days=1))


def _safe(value: str) -> str:
    """姓名进文件名前的清洗:中文与字母数字保留,其余换下划线。"""
    return "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in value)


def _department_name(person: Person, organization: Organization) -> str:
    department = organization.department_map().get(person.department_id or "")
    return department.name if department else ""


class WorkBuddyInbox:
    """待写回的提交暂存:**一人一个文件,以姓名为文件名**,重复提交覆盖,写回成功后删除。

    目录里只剩"还没进飞书表"的东西,所以始终很小(一天最多一份人数量级)。
    文件名不带日期——日期以文件里的 ``target_date`` 为准,写回时只取当天那份。
    姓名按约定唯一(重名在认人那一步就被拒了),所以用它做文件名是安全的。
    """

    def __init__(self, state_dir: str | Path) -> None:
        self.root = Path(state_dir) / "workbuddy_inbox"

    def path(self, name: str) -> Path:
        return self.root / f"{_safe(name)}.json"

    def save(self, submission: WorkBuddySubmission) -> Path:
        path = self.path(submission.name)
        FileStateStore._atomic_write(path, submission.model_dump(mode="json"))
        return path

    def pending(self, target_date: date) -> dict[str, WorkBuddySubmission]:
        """该日期尚未写回的提交,每人的最后一笔。"""
        if not self.root.is_dir():
            return {}
        latest: dict[str, WorkBuddySubmission] = {}
        for path in sorted(self.root.glob("*.json")):
            try:
                submission = WorkBuddySubmission.model_validate(
                    json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                # 暂存读不出来就不能建快照:宁可整日推迟,也不把交过的人写成未交。
                raise ValueError(f"提交缓存文件损坏:{path}") from exc
            if submission.target_date != target_date:
                # 别的日期还没写回的旧文件,跳过(不会拿它当今天的日志写进表)。
                continue
            latest[submission.person_id] = submission
        return latest

    def discard(self, submission: WorkBuddySubmission) -> None:
        """写回成功(或已确认写回过)后清掉暂存。"""
        try:
            self.path(submission.name).unlink()
        except FileNotFoundError:
            pass


class LogSubmitService:
    """对话入口的两个动作:认人(resolve)与收日志(submit),外加写回(flush)。"""

    def __init__(self, *, inbox: WorkBuddyInbox,
                 organization_cache: OrganizationCache,
                 bitable: BitableService,
                 table_config: TableConfig,
                 workflow_store: FileStateStore,
                 settings: LogSubmitSettings) -> None:
        self.inbox = inbox
        self.organization_cache = organization_cache
        self.bitable = bitable
        self.logs_table = table_config.tables["logs"]
        self.workflow_store = workflow_store
        self.submit_opens_at = settings.submit_opens_at
        self.submit_closes_at = settings.submit_closes_at
        self.fallback_form_url = settings.fallback_form_url
        self.archive_at = settings.archive_at

    # ------------------------------------------------------------------ 认人

    def resolve(self, name: str) -> dict[str, Any]:
        organization = self.organization_cache.get()
        matches = organization.persons_named(name)
        if len(matches) != 1:
            if len(matches) > 1:
                # 姓名按约定唯一;真撞上了就不能猜,记日志等运维改组织表。
                logger.warning("workbuddy_ambiguous_name name=%s person_ids=%s",
                               name, [person.person_id for person in matches])
            return {"code": NO_PERSON, "name": name.strip()}
        person = matches[0]
        if person.role not in SUPPORTED_ROLES:
            return {"code": ROLE_NOT_SUPPORTED, "person_id": person.person_id,
                    "name": person.name, "role": person.role}
        return {"code": OK, "person_id": person.person_id, "name": person.name,
                "role": person.role,
                "department_name": _department_name(person, organization),
                "submit_window": self.submit_window(),
                "fallback_form_url": self.fallback_form_url}

    # ---------------------------------------------------------------- 收日志

    def submit(self, request: WorkBuddySubmitRequest) -> dict[str, Any]:
        organization = self.organization_cache.get()
        person, failure = self._identify(request, organization)
        if failure is not None:
            return failure
        assert person is not None
        window = self.submit_window()
        if window["state"] == "closed":
            return {"code": CLOSED, "person_id": person.person_id,
                    "next_open_at": window["next_open_at"]}
        if request.content_is_empty():
            return {"code": EMPTY_CONTENT, "person_id": person.person_id}
        now = datetime.now(SHANGHAI)
        submission = WorkBuddySubmission(
            submission_id=str(uuid4()),
            person_id=person.person_id,
            name=person.name,
            role=person.role,
            department_name=_department_name(person, organization),
            submitted_at=now.astimezone(timezone.utc),
            target_date=now.date(),
            progress=request.progress.strip(),
            difficulties=request.difficulties.strip(),
            reflection=request.reflection.strip(),
            other=request.other.strip(),
        )
        path = self.inbox.save(submission)
        logger.info("workbuddy_submission_stored person_id=%s date=%s file=%s",
                    person.person_id, submission.target_date, path.name)
        return {"code": OK, "person_id": person.person_id, "name": person.name,
                "role": person.role, "department_name": submission.department_name,
                "log_date": submission.target_date.isoformat(),
                "archive_at": self._archive_at(submission.target_date)}

    def submit_window(self, now: datetime | None = None) -> dict[str, Any]:
        """提交窗口状态。服务端是唯一裁判,入口只负责照着念。"""
        current = now or datetime.now(SHANGHAI)
        opens = self._moment(current.date(), self.submit_opens_at)
        closes = self._moment(current.date(), self.submit_closes_at)
        if opens <= current < closes:
            state, next_open = "open", opens
        elif current < opens:
            state, next_open = "closed", opens
        else:
            state, next_open = "closed", opens + timedelta(days=1)
        return {"state": state, "opens_at": self.submit_opens_at,
                "closes_at": self.submit_closes_at,
                "next_open_at": next_open.isoformat()}

    def _identify(self, request: WorkBuddySubmitRequest,
                  organization: Organization,
                  ) -> tuple[Person | None, dict[str, Any] | None]:
        person_id = (request.person_id or "").strip()
        name = (request.name or "").strip()
        if person_id:
            person = organization.person_map().get(person_id)
            if (person is None or not person.active
                    or (name and person.name.strip() != name)):
                return None, {"code": PERSON_ID_UNKNOWN, "person_id": person_id}
        else:
            matches = organization.persons_named(name)
            if len(matches) != 1:
                if len(matches) > 1:
                    logger.warning("workbuddy_ambiguous_name name=%s person_ids=%s",
                                   name, [person.person_id for person in matches])
                return None, {"code": NO_PERSON, "name": name}
            person = matches[0]
        if person.role not in SUPPORTED_ROLES:
            return None, {"code": ROLE_NOT_SUPPORTED, "person_id": person.person_id,
                          "name": person.name, "role": person.role}
        return person, None

    # ------------------------------------------------------------------ 写回

    def flush(self, target_date: date) -> tuple[int, dict[str, str]]:
        """把某天的暂存写回日志表,返回(新建行数, 需登记的问题)。

        幂等:业务键记在 ``external_records`` 里;键丢了但行已存在的,靠
        (姓名, 提交时间)认领,不重复建行。写回成功后删掉暂存文件。
        """
        pending = self.inbox.pending(target_date)
        if not pending:
            return 0, {}
        organization = self.organization_cache.get()
        people = organization.person_map()
        known = self._rows_by_submitter(target_date)
        creates: list[tuple[str, WorkBuddySubmission, dict[str, Any]]] = []
        issues: dict[str, str] = {}
        cleaned = 0
        for person_id in sorted(pending):
            submission = pending[person_id]
            business_key = f"{_BUSINESS_KEY_PREFIX}:{person_id}"
            if self.workflow_store.get_external_record(target_date, business_key):
                # 上一轮已经落过表,只是没来得及清暂存。
                self.inbox.discard(submission)
                cleaned += 1
                continue
            person = people.get(person_id)
            if person is None or not person.active:
                # 这份暂存不删:等人补回组织表之后还能写回。
                issues[f"workbuddy:{person_id}:person-missing"] = (
                    f"「{submission.name}」已不在人员表,这笔日志未写回")
                continue
            found = known.get((person.name, _millis(submission.submitted_at)), [])
            if len(found) == 1:
                # 上一轮已经建过这一行(或者建完就中断了),认领它。
                self.workflow_store.map_external_record(
                    target_date, business_key, "logs", found[0])
                self.inbox.discard(submission)
                cleaned += 1
                continue
            if len(found) > 1:
                issues[f"workbuddy:{person_id}:duplicate-rows"] = (
                    f"「{person.name}」当日已有 {len(found)} 行提交时间相同的日志,"
                    "本次仍按最后一笔写入,请核对原表")
            creates.append((business_key, submission,
                            self._fields(submission, person)))
        if creates:
            self._create_rows(target_date, creates)
        if cleaned:
            logger.info("workbuddy_inbox_reconciled date=%s count=%d",
                        target_date, cleaned)
        return len(creates), issues

    def _create_rows(self, target_date: date,
                     creates: list[tuple[str, WorkBuddySubmission, dict[str, Any]]]) -> None:
        table_id = self.logs_table.table_id
        for offset in range(0, len(creates), _BATCH_SIZE):
            chunk = creates[offset:offset + _BATCH_SIZE]
            created = self.bitable.batch_create(
                table_id, [fields for _, _, fields in chunk])
            if len(created) != len(chunk):
                raise ValueError("飞书批量创建日志返回数量不一致")
            for (business_key, submission, _), record in zip(chunk, created, strict=True):
                record_id = str(record.get("record_id", ""))
                if not record_id:
                    raise ValueError("日志创建后没有记录 ID")
                self.workflow_store.map_external_record(
                    target_date, business_key, "logs", record_id)
                # 落到飞书、并且登记了业务键之后,才丢暂存。
                self.inbox.discard(submission)

    def _rows_by_submitter(self, target_date: date) -> dict[tuple[str, int], list[str]]:
        """当日日志表里 (姓名, 提交时间毫秒) → record_id。

        只认「提交时间」这一列:``record_submission_time`` 会掺
        ``last_modified_time``,人工编辑就会把旧行判成更新的。
        """
        fields = self.logs_table.fields
        low, high = _day_bounds_ms(target_date)
        rows: dict[tuple[str, int], list[str]] = {}
        for record in self.bitable.iter_records(
                self.logs_table.table_id,
                field_names=[fields["submitter_name"], fields["submitted_at"]]):
            values = record_fields(record)
            stamp = _field_millis(values.get(fields["submitted_at"]))
            record_id = str(record.get("record_id", ""))
            if stamp is None or not (low <= stamp < high) or not record_id:
                continue
            name = scalar(values.get(fields["submitter_name"])).strip()
            if name:
                rows.setdefault((name, stamp), []).append(record_id)
        return rows

    def _fields(self, submission: WorkBuddySubmission,
                person: Person) -> dict[str, Any]:
        fields = self.logs_table.fields
        row: dict[str, Any] = {
            fields["submitted_at"]: _millis(submission.submitted_at),
            fields["submitter_name"]: person.name,
            fields["progress"]: submission.progress,
            fields["difficulties"]: submission.difficulties,
            fields["reflection"]: submission.reflection,
            fields["other"]: submission.other,
            fields["full_log"]: WorkLog.compose(
                submission.progress, submission.difficulties,
                submission.reflection, submission.other),
        }
        # 空栏位不写:别拿空字符串去覆盖表里已有的内容。
        row = {key: value for key, value in row.items() if value != ""}
        if person.open_id:
            row[fields["submitter_ref"]] = [{"id": person.open_id}]
        return row

    def _archive_at(self, target_date: date) -> str:
        return self._moment(target_date + timedelta(days=1),
                            self.archive_at).isoformat()

    @staticmethod
    def _moment(day: date, hhmm: str) -> datetime:
        hour, minute = (int(part) for part in hhmm.split(":", 1))
        return datetime.combine(day, time(hour, minute), SHANGHAI)

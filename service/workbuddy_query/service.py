"""日志 / 评价 / 阶段工作查询：只读实现，供对话入口按权限取数。

数据来源全部复用既有读取器，绝不写飞书、不改状态：
- 历史日志：``LogRepository.logs_in_window``
- 已定稿评价：本地快照 ``load_check_snapshot`` 优先，缺的天用 ``SnapshotRebuilder`` 重建
- 今天（评价模式）：``WorkBuddyInbox.pending`` ∪ 飞书今天已归档的日志
- 阶段产出：``StageReader``（表驱动）
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from data.repositories import LogRepository, OrganizationCache
from data.store import FileStateStore
from schema import TableConfig
from schema.domain import Organization, Person
from tool.feishu import BitableService
from workflow1.models import UnitStatus
from workflow1.workbuddy_inbox import WorkBuddyInbox
from workflow2.feishu_material import SnapshotRebuilder

from service.workbuddy_query.cache import TtlCache
from service.workbuddy_query.cursor import decode_cursor, encode_cursor
from service.workbuddy_query.models import (
    CONTENT_NOT_SUPPORTED,
    DUPLICATE_NAME,
    NO_PERSON,
    OK,
    OUT_OF_SCOPE,
    QUERY_FAILED,
    ROLE_NOT_ALLOWED,
    QueryError,
    QueryRequest,
)
from service.workbuddy_query.permissions import (
    ROLE_BACKBONE,
    ROLE_LEADER,
    ROLE_MINISTER,
    ScopeResolver,
    scope_of,
)
from service.workbuddy_query.ranges import clamp_view_window, resolve_window
from service.workbuddy_query.stage import StageReader

logger = logging.getLogger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")
QUERY_PAGE_SIZE = 50
STAGE_ROLES = frozenset({ROLE_MINISTER, ROLE_LEADER})
REVIEW_ROLES = frozenset({ROLE_BACKBONE, ROLE_MINISTER, ROLE_LEADER})
_SOURCE_LABELS = {"HUMAN": "人工", "AI": "AI"}


def _contiguous_spans(days: list[date]) -> list[tuple[date, date]]:
    spans: list[tuple[date, date]] = []
    for day in sorted(days):
        if spans and day == spans[-1][1] + timedelta(days=1):
            spans[-1] = (spans[-1][0], day)
        else:
            spans.append((day, day))
    return spans


class WorkBuddyQueryService:
    def __init__(self, *, organization_cache: OrganizationCache,
                 log_repository: LogRepository, bitable: BitableService,
                 tables: TableConfig, inbox: WorkBuddyInbox,
                 snapshot_store: FileStateStore, rebuilder: SnapshotRebuilder,
                 cursor_secret: str,
                 clock: Callable[[], datetime] | None = None,
                 read_cache_ttl_seconds: float = 30.0,
                 page_size: int = QUERY_PAGE_SIZE) -> None:
        self.organization_cache = organization_cache
        self.log_repository = log_repository
        self.bitable = bitable
        self.tables = tables
        self.inbox = inbox
        self.snapshot_store = snapshot_store
        self.rebuilder = rebuilder
        self.cursor_secret = cursor_secret
        self._clock = clock or (lambda: datetime.now(SHANGHAI))
        self._cache = TtlCache(read_cache_ttl_seconds)
        self.page_size = page_size

    # ------------------------------------------------------------- 认人

    def identity(self, name: str) -> dict:
        try:
            return self._identity(name)
        except QueryError as exc:
            return exc.payload()
        except Exception:
            logger.exception("query_identity_failed name=%s", name)
            return {"code": QUERY_FAILED}

    def _identity(self, name: str) -> dict:
        organization = self.organization_cache.get()
        matches = organization.persons_named(name)
        if not matches:
            return {"code": NO_PERSON, "name": name.strip()}
        if len(matches) > 1:
            logger.warning("query_ambiguous_name name=%s matches=%d", name, len(matches))
            return {"code": DUPLICATE_NAME, "name": name.strip()}
        person = matches[0]
        department = organization.department_map().get(person.department_id or "")
        return {
            "code": OK,
            "person_id": person.person_id,
            "name": person.name,
            "role": person.role,
            "department_name": department.name if department else "",
            "scope": scope_of(person.role),
        }

    # ------------------------------------------------------------- 查询

    def query(self, request: QueryRequest) -> dict:
        try:
            return self._query(request)
        except QueryError as exc:
            return exc.payload()
        except Exception:
            # 数据源读取等非业务异常也统一成 QUERY_FAILED，别把 500 甩给入口。
            logger.exception("query_failed person_id=%s", request.person_id)
            return {"code": QUERY_FAILED}

    def _query(self, request: QueryRequest) -> dict:
        today = self._clock().date()
        organization = self.organization_cache.get()
        person = organization.person_map().get(request.person_id)
        if person is None or not person.active:
            raise QueryError(OUT_OF_SCOPE)
        resolver = ScopeResolver(organization)
        if request.mode == "review":
            return self._review(request, organization, person, resolver, today)
        if request.content == "stage":
            return self._stage(request, organization, person, resolver, today)
        return self._view_logs(request, organization, person, resolver, today)

    # --------------------------------------------------------- 查看·日志

    def _view_logs(self, request: QueryRequest, organization: Organization,
                   person: Person, resolver: ScopeResolver, today: date) -> dict:
        start, end = resolve_window(request.range, today=today)
        start, end = clamp_view_window(start, end, today=today)
        target_ids = resolver.resolve_target(request.target, person)
        items = self._log_items(start, end, target_ids, organization)
        roster = self._roster(organization, target_ids)
        days = (end - start).days + 1
        summary = {
            "persons": len(roster),
            "days": days,
            "submitted": len(items),
            "missing": max(0, len(roster) * days - len(items)),
        }
        return self._respond(start, end, summary, roster, items, request.cursor)

    def _log_items(self, start: date, end: date, target_ids: set[str],
                   organization: Organization) -> list[dict]:
        people = organization.person_map()
        logs_by_day = self._logs(start, end, organization)
        snapshots = self._snapshots(start, end, organization)
        items: list[dict] = []
        day = start
        while day <= end:
            logs, _ = logs_by_day.get(day, ([], {}))
            snapshot = snapshots.get(day)
            for log in logs:
                if log.person_id not in target_ids:
                    continue
                person = people.get(log.person_id)
                item = {
                    "item_id": log.log_id,
                    "date": day.isoformat(),
                    "name": person.name if person else log.person_id,
                    "log": {"progress": log.progress, "difficulties": log.difficulties,
                            "reflection": log.reflection, "other": log.other},
                }
                evaluation = self._evaluation(snapshot, log.log_id)
                if evaluation is not None:
                    item["evaluation"] = evaluation
                items.append(item)
            day += timedelta(days=1)
        items.sort(key=lambda item: (item["date"], item["name"]))
        return items

    @staticmethod
    def _evaluation(snapshot, log_id: str) -> dict | None:
        if snapshot is None:
            return None
        state = snapshot.log_evaluations.get(log_id)
        if state is None or state.status != UnitStatus.CONFIRMED:
            return None
        positive = (state.positive_final or "").strip()
        improvement = (state.improvement_final or "").strip()
        if not positive or not improvement:
            return None
        return {"positive": positive, "improvement": improvement,
                "source": _SOURCE_LABELS.get(state.source, state.source)}

    # ------------------------------------------------------- 评价·今天

    def _review(self, request: QueryRequest, organization: Organization,
                person: Person, resolver: ScopeResolver, today: date) -> dict:
        if person.role not in REVIEW_ROLES:
            raise QueryError(ROLE_NOT_ALLOWED)
        target_ids = (resolver.resolve_target(request.target, person)
                      if request.target is not None
                      else resolver.direct_reports(person))
        items = self._today_items(today, target_ids, organization)
        roster = self._roster(organization, target_ids)
        summary = {
            "persons": len(roster),
            "days": 1,
            "submitted": len(items),
            "missing": max(0, len(roster) - len(items)),
        }
        return self._respond(today, today, summary, roster, items, request.cursor)

    def _today_items(self, today: date, target_ids: set[str],
                     organization: Organization) -> list[dict]:
        people = organization.person_map()
        by_person: dict[str, dict] = {}
        logs_today, _ = self._logs(today, today, organization).get(today, ([], {}))
        for log in logs_today:
            if log.person_id in target_ids:
                by_person[log.person_id] = self._log_item(log, today, people)
        # 暂存是"还没进表"的最新一版，覆盖同日已归档的记录。
        for person_id, submission in self.inbox.pending(today).items():
            if person_id not in target_ids:
                continue
            person = people.get(person_id)
            by_person[person_id] = {
                "item_id": submission.submission_id,
                "date": today.isoformat(),
                "name": person.name if person else submission.name,
                "log": {"progress": submission.progress,
                        "difficulties": submission.difficulties,
                        "reflection": submission.reflection,
                        "other": submission.other},
            }
        return [by_person[pid] for pid in sorted(by_person)]

    @staticmethod
    def _log_item(log, day: date, people: dict) -> dict:
        person = people.get(log.person_id)
        return {
            "item_id": log.log_id,
            "date": day.isoformat(),
            "name": person.name if person else log.person_id,
            "log": {"progress": log.progress, "difficulties": log.difficulties,
                    "reflection": log.reflection, "other": log.other},
        }

    # ------------------------------------------------------------- 阶段

    def _stage(self, request: QueryRequest, organization: Organization,
               person: Person, resolver: ScopeResolver, today: date) -> dict:
        if person.role not in STAGE_ROLES:
            raise QueryError(CONTENT_NOT_SUPPORTED)
        start, end = resolve_window(request.range, today=today)
        start, end = clamp_view_window(start, end, today=today)
        target_ids = resolver.resolve_target(request.target, person)
        is_leader = person.role == ROLE_LEADER
        # 负责人的权限上界是全体；只有明确指定了部门/个人/小组才收窄本次范围。
        narrowed = request.target is not None and request.target.type != "self"
        if is_leader and not narrowed:
            department_ids, include_team = None, True
        else:
            department_ids, include_team = resolver.department_ids(target_ids), False
        reader = StageReader(self.bitable, self.tables, organization, cache=self._cache)
        records = reader.read(start, end, department_ids=department_ids,
                              include_team=include_team)
        items = [{"item_id": record.item_id, "date": record.date.isoformat(),
                  "name": record.name, "kind": "stage",
                  "sections": [dict(section) for section in record.sections]}
                 for record in records]
        summary = {
            "persons": len({record.name for record in records}),
            "days": (end - start).days + 1,
            "submitted": len(items),
            "missing": 0,
        }
        return self._respond(start, end, summary, [], items, request.cursor)

    # --------------------------------------------------------- 公共底座

    def _roster(self, organization: Organization, target_ids: set[str]) -> list[dict]:
        people = organization.person_map()
        departments = organization.department_map()
        roster = []
        for person_id in sorted(target_ids):
            person = people.get(person_id)
            if person is None or not person.active:
                continue
            department = departments.get(person.department_id or "")
            roster.append({"person_id": person.person_id, "name": person.name,
                           "role": person.role,
                           "department_name": department.name if department else ""})
        return roster

    def _logs(self, start: date, end: date, organization: Organization) -> dict:
        return self._cache.get_or_load(
            ("logs", start, end),
            lambda: self.log_repository.logs_in_window(start, end, organization))

    def _snapshots(self, start: date, end: date, organization: Organization) -> dict:
        snapshots: dict[date, object] = {}
        missing: list[date] = []
        day = start
        while day <= end:
            try:
                snapshot = self.snapshot_store.load_check_snapshot(day)
            except Exception:
                logger.exception("query_snapshot_load_failed date=%s", day)
                snapshot = None
            if snapshot is None:
                missing.append(day)
            else:
                snapshots[day] = snapshot
            day += timedelta(days=1)
        for span_start, span_end in _contiguous_spans(missing):
            snapshots.update(self._cache.get_or_load(
                ("rebuild", span_start, span_end),
                lambda s=span_start, e=span_end: self.rebuilder.rebuild(s, e)))
        return snapshots

    def _respond(self, start: date, end: date, summary: dict, roster: list[dict],
                 items: list[dict], cursor: str | None) -> dict:
        offset = decode_cursor(cursor, self.cursor_secret) if cursor else 0
        page = items[offset:offset + self.page_size]
        next_offset = offset + self.page_size
        next_cursor = (encode_cursor(next_offset, self.cursor_secret)
                       if next_offset < len(items) else None)
        return {
            "code": OK,
            "range": {"from": start.isoformat(), "to": end.isoformat()},
            "summary": summary,
            "roster": roster,
            "items": page,
            "next_cursor": next_cursor,
        }

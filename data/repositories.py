from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta
from typing import Any

from data.errors import FeishuReadError
from schema import (
    Department,
    Organization,
    Person,
    TableConfig,
    WorkLog,
    LogResource,
)
from data import FileStateStore
from tool.bitable_fields import (
    SHANGHAI,
    alias_index,
    display_name,
    field_boolean,
    field_datetime,
    record_fields,
    references,
    resolve_reference,
    scalar,
)
from tool.feishu import BitableService, ContactService

logger = logging.getLogger(__name__)


class OrganizationRepository:
    def __init__(self, bitable: BitableService, contacts: ContactService,
                 config: TableConfig, *, use_person_field_ids: bool = False) -> None:
        self.bitable = bitable
        self.contacts = contacts
        self.config = config
        self.use_person_field_ids = use_person_field_ids

    def load(self) -> Organization:
        departments, persons = self._read_organization()
        anomalies = self._find_anomalies(departments, persons)
        team_leaders = [person.person_id for person in persons if person.role == "团队负责人"]
        return Organization(
            persons=persons,
            departments=departments,
            team_leader_id=team_leaders[0] if len(team_leaders) == 1 else None,
            anomalies=anomalies,
        )

    def _read_organization(self) -> tuple[list[Department], list[Person]]:
        try:
            departments_table = self.config.tables["departments"]
            persons_table = self.config.tables["persons"]
            department_records = self.bitable.list_records(departments_table.table_id)
            person_records = self.bitable.list_records(persons_table.table_id)
            if self.use_person_field_ids:
                return self._parse_organization(
                    departments_table.fields, department_records,
                    persons_table.fields, person_records,
                    use_person_field_ids=True,
                )
            mobile_field = persons_table.fields["mobile"]
            person_mobiles = [
                (record, scalar(record_fields(record).get(mobile_field)).strip())
                for record in person_records
                if scalar(record_fields(record).get(persons_table.fields["role"])).strip()
                != "基层学生"
            ]
            person_mobiles = [(record, mobile) for record, mobile in person_mobiles
                              if mobile]
            mobiles = list(dict.fromkeys(mobile for _, mobile in person_mobiles))
            if len(mobiles) != len(person_mobiles):
                raise ValueError("人员表存在重复手机号，请确保每个人填写不同的手机号")
            mobile_open_ids: dict[str, str] = {}
            for offset in range(0, len(mobiles), 50):
                for user in self.contacts.batch_get_ids(
                    mobiles=mobiles[offset:offset + 50], user_id_type="open_id"
                ):
                    mobile = str(user.get("mobile") or "").strip()
                    open_id = str(user.get("user_id") or "").strip()
                    if mobile and open_id:
                        mobile_open_ids[mobile] = open_id
            # 基层学生只填写日志，不占用飞书组织名额，也没有 OpenID。
            # 其他角色继续使用原有的手机号 -> OpenID 身份流程。
            person_records = [
                record for record in person_records
                if scalar(record_fields(record).get(persons_table.fields["role"])).strip()
                == "基层学生"
                or scalar(record_fields(record).get(mobile_field)).strip()
                in mobile_open_ids
            ]
            return self._parse_organization(
                departments_table.fields,
                department_records,
                persons_table.fields,
                person_records,
                mobile_open_ids,
            )
        except Exception as exc:
            raise FeishuReadError(f"Unable to refresh organization cache: {exc}") from exc

    @staticmethod
    def _parse_organization(
        department_fields,
        department_records,
        person_fields,
        person_records,
        mobile_open_ids=None,
        use_person_field_ids=False,
    ):
        mobile_open_ids = mobile_open_ids or {}
        def person_open_id(item, values) -> str:
            if use_person_field_ids:
                return scalar(values.get(person_fields["name"])).strip()
            return mobile_open_ids.get(
                scalar(values.get(person_fields["mobile"])).strip(), ""
            )
        department_rows = []
        for item in department_records:
            values = record_fields(item)
            department_id = scalar(
                values.get(department_fields["department_id"])
            ).strip()
            department_rows.append((item, values, department_id))
        department_aliases = alias_index(
            (
                department_id,
                (
                    department_id,
                    str(item.get("record_id", "")),
                    scalar(values.get(department_fields["name"])),
                ),
            )
            for item, values, department_id in department_rows
        )

        person_rows = []
        for item in person_records:
            values = record_fields(item)
            person_id = (scalar(values.get(person_fields["person_id"])).strip()
                         or str(item.get("record_id", "")))
            person_rows.append((item, values, person_id))
        person_aliases = alias_index(
            (
                person_id,
                (
                    person_id,
                    str(item.get("record_id", "")),
                    display_name(values.get(person_fields["name"])),
                    person_open_id(item, values),
                ),
            )
            for item, values, person_id in person_rows
        )

        persons = []
        for item, values, person_id in person_rows:
            role = scalar(values.get(person_fields["role"]))
            leader_id = resolve_reference(
                values.get(person_fields["leader_ref"]), person_aliases
            )
            if not leader_id and role == "骨干学生":
                leader_id = resolve_reference(
                    values.get(person_fields["minister_ref"]), person_aliases
                )
            persons.append(
                Person(
                    person_id=person_id,
                    name=display_name(values.get(person_fields["name"])),
                    role=role,
                    department_id=resolve_reference(
                        values.get(person_fields["department_ref"]),
                        department_aliases,
                    )
                    or None,
                    leader_id=leader_id or None,
                    open_id=person_open_id(item, values) or None,
                    mobile=scalar(values.get(person_fields["mobile"])).strip() or None,
                    source_record_id=str(item.get("record_id", "")) or None,
                    active=field_boolean(values.get(person_fields.get("active"))),
                )
            )

        departments = []
        for item, values, department_id in department_rows:
            minister_id = resolve_reference(
                values.get(department_fields["minister_ref"]), person_aliases
            )
            if not minister_id:
                candidates = [
                    person.person_id
                    for person in persons
                    if person.role == "部长"
                    and person.department_id == department_id
                    and person.active
                ]
                minister_id = candidates[0] if len(candidates) == 1 else ""
            departments.append(
                Department(
                    department_id=department_id,
                    name=scalar(values.get(department_fields["name"]))
                    or department_id,
                    minister_id=minister_id or None,
                    source_record_id=str(item.get("record_id", "")) or None,
                    active=field_boolean(values.get(department_fields.get("active"))),
                )
            )
        return departments, persons

    @staticmethod
    def _find_anomalies(departments, persons) -> list[str]:
        anomalies: list[str] = []
        basic_names: set[str] = set()
        for person in persons:
            if person.active and person.role == "基层学生":
                name = person.name.strip()
                if name in basic_names:
                    anomalies.append(f"基层学生姓名「{name}」重复，无法仅用姓名归属日志")
                basic_names.add(name)
        person_by_id = {person.person_id: person for person in persons if person.person_id}
        person_ids = set(person_by_id)
        department_ids = {item.department_id for item in departments if item.department_id}
        for person in persons:
            anomalies.extend(
                OrganizationRepository._person_anomalies(
                    person, person_by_id, person_ids, department_ids
                )
            )
        for department in departments:
            if department.active and department.minister_id not in person_ids:
                anomalies.append(f"部门 {department.department_id} 缺少有效部长")
        if sum(person.role == "团队负责人" for person in persons) != 1:
            anomalies.append("必须且只能配置一名团队负责人")
        return anomalies

    @staticmethod
    def _person_anomalies(person, person_by_id, person_ids, department_ids):
        anomalies = []
        label = f"人员「{person.name or '未知'}」（ID：{person.person_id or '未填'}）"
        if not person.person_id or not person.name or not person.role:
            anomalies.append(f"人员记录缺少编号、姓名或角色: {person.name or '未知'}")
        if person.department_id and person.department_id not in department_ids:
            anomalies.append(f"{label} 的部门不存在")
        if person.leader_id and person.leader_id not in person_ids:
            anomalies.append(f"{label} 的直属上级不存在")
        if person.active and person.role in {"基层学生", "骨干学生"}:
            if not person.leader_id:
                anomalies.append(f"{label} 没有直属上级")
            elif person.leader_id in person_ids:
                expected = "骨干学生" if person.role == "基层学生" else "部长"
                if person_by_id[person.leader_id].role != expected:
                    anomalies.append(
                        f"{label} 的直属上级角色应为 {expected}"
                    )
        return anomalies


class OrganizationCache:
    """Persisted organization master copy.

    The Feishu person/department tables are read only on the first load
    (service startup or the lazy first call) and via an explicit ``refresh()``.
    Runtime changes go through the person CRUD helpers below, which mutate
    only the persisted local cache.
    """

    def __init__(self, store: FileStateStore, repository: OrganizationRepository) -> None:
        self.store = store
        self.repository = repository
        self._lock = threading.RLock()

    def get(self) -> Organization:
        with self._lock:
            cached = self.store.load_cache("organization")
            if cached is not None:
                return Organization.model_validate(cached)
            return self.refresh()

    def refresh(self) -> Organization:
        with self._lock:
            organization = self.repository.load()
            self.store.save_cache("organization", organization.model_dump(mode="json"))
            logger.info("organization_cache_refreshed persons=%d departments=%d anomalies=%d",
                        len(organization.persons), len(organization.departments),
                        len(organization.anomalies))
            return organization

    def add_person(self, person: Person) -> Organization:
        def mutate(organization: Organization) -> None:
            if any(item.person_id == person.person_id for item in organization.persons):
                raise ValueError(f"人员 {person.person_id} 已存在")
            organization.persons.append(person)

        return self._mutate(mutate)

    def update_person(self, person_id: str, changes: Mapping[str, Any]) -> Organization:
        def mutate(organization: Organization) -> None:
            person = next((item for item in organization.persons
                           if item.person_id == person_id), None)
            if person is None:
                raise LookupError(f"人员 {person_id} 不存在")
            for field, value in changes.items():
                setattr(person, field, value)

        return self._mutate(mutate)

    def delete_person(self, person_id: str) -> Organization:
        def mutate(organization: Organization) -> None:
            remaining = [item for item in organization.persons
                         if item.person_id != person_id]
            if len(remaining) == len(organization.persons):
                raise LookupError(f"人员 {person_id} 不存在")
            organization.persons = remaining

        return self._mutate(mutate)

    def _mutate(self, mutator: Callable[[Organization], None]) -> Organization:
        with self._lock:
            organization = self.get()
            mutator(organization)
            team_leaders = [person.person_id for person in organization.persons
                            if person.role == "团队负责人"]
            organization.team_leader_id = team_leaders[0] if len(team_leaders) == 1 else None
            organization.anomalies = OrganizationRepository._find_anomalies(
                organization.departments, organization.persons)
            self.store.save_cache("organization", organization.model_dump(mode="json"))
            return organization


class LogRepository:
    def __init__(
        self,
        bitable: BitableService,
        config: TableConfig,
        organization_cache: OrganizationCache,
    ) -> None:
        self.bitable = bitable
        self.table = config.tables["logs"]
        self.organization_cache = organization_cache

    def backfill_full_logs(self, logs: list[WorkLog]) -> int:
        """Write the composed text to the source table when it differs."""
        field = self.table.fields["full_log"]
        updates = [
            {"record_id": log.source_record_id,
             "fields": {field: log.content()}}
            for log in logs
            if log.source_record_id and log.content() != log.full_log
        ]
        for offset in range(0, len(updates), 500):
            self.bitable.batch_update(self.table.table_id, updates[offset:offset + 500])
        return len(updates)

    def get_logs_by_date(
        self, target_date: date,
        organization: Organization | None = None,
    ) -> tuple[list[WorkLog], dict[str, str]]:
        """Return the day's logs (one per person, latest wins) plus read issues.

        Broken rows are skipped and reported through the issue ledger instead
        of failing the whole day: missing timestamps are interpolated from the
        neighbouring record (tables are created in submission order), and
        submitters absent from the persons table are recorded and skipped.
        """
        bucket = self._logs_in_window(target_date, target_date, organization)
        return bucket.get(target_date, ([], {}))

    def logs_in_window(
        self, start: date, end: date,
        organization: Organization | None = None,
    ) -> dict[date, tuple[list[WorkLog], dict[str, str]]]:
        """Read the log table once and return (logs, issues) per day in range.

        Identical parsing semantics as ``get_logs_by_date``; used by Workflow2
        when the local snapshot cache no longer covers the analysis window.
        """
        return self._logs_in_window(start, end, organization)

    def _logs_in_window(
        self, start_day: date, end_day: date,
        organization: Organization | None = None,
    ) -> dict[date, tuple[list[WorkLog], dict[str, str]]]:
        start = datetime.combine(start_day, time.min, SHANGHAI)
        end = datetime.combine(end_day + timedelta(days=1), time.min, SHANGHAI)
        issues: dict[str, str] = {}
        try:
            organization = organization or self.organization_cache.get()
            person_aliases = alias_index(
                (
                    person.person_id,
                    (
                        person.person_id,
                        person.name,
                        person.open_id or "",
                        person.source_record_id or "",
                    ),
                )
                for person in organization.persons
            )
            basic_names = alias_index(
                (person.person_id, (person.name,))
                for person in organization.persons
                if person.active and person.role == "基层学生"
            )
            records = self.bitable.list_records(
                self.table.table_id,
                field_names=list(self.table.fields.values()),
            )
            f = self.table.fields
            rows: list[dict] = []
            for item in records:
                values = record_fields(item)
                log_id = scalar(values.get(f["log_id"])) or str(
                    item.get("record_id", "")
                )
                try:
                    submitted_at = field_datetime(values.get(f["submitted_at"]))
                except (ValueError, TypeError):
                    submitted_at = None
                rows.append({
                    "item": item, "values": values, "log_id": log_id,
                    "submitted_at": submitted_at,
                })
            # Neighbour-based interpolation keeps submission order semantics.
            resolved: list[datetime | None] = [row["submitted_at"] for row in rows]
            for index, row in enumerate(rows):
                if row["submitted_at"] is not None:
                    continue
                timestamp = next(
                    (value for value in reversed(resolved[:index]) if value), None)
                if timestamp is None:
                    timestamp = next(
                        (value for value in resolved[index + 1:] if value), None)
                if timestamp is None:
                    timestamp = datetime.now(SHANGHAI)
                resolved[index] = timestamp
                issues[f"log:{row['log_id']}:time-interpolated"] = (
                    "日志缺少提交时间，按邻近记录时间归入处理，请核对原表")
            per_day: dict[date, tuple[list[WorkLog], dict[str, str]]] = {}
            day = start_day
            while day <= end_day:
                per_day[day] = ([], dict(issues))
                day += timedelta(days=1)
            result: list[WorkLog] = []
            for row, submitted_at in zip(rows, resolved):
                if not start <= submitted_at < end:
                    continue
                values = row["values"]
                named = scalar(values.get(f.get("submitter_name", ""))).strip()
                referenced = resolve_reference(values.get(f["submitter_ref"]), person_aliases)
                if named:
                    person_id = basic_names.get(named, "")
                    if not person_id and referenced:
                        # 姓名栏填了但不在基层名单里：骨干/部长用 OpenID 提交时
                        # 也会填姓名，提交人已解析到人员，以提交人为准。
                        person_id = referenced
                    elif not person_id or (referenced and referenced != person_id):
                        issues[f"log:{row['log_id']}:unknown-submitter"] = (
                            f"日志 {row['log_id']} 的基层姓名「{named}」无法唯一匹配人员表"
                            "或与提交人字段冲突，本条已跳过")
                        row_day = submitted_at.astimezone(SHANGHAI).date()
                        if row_day in per_day:
                            per_day[row_day][1][f"log:{row['log_id']}:unknown-submitter"] = (
                                issues[f"log:{row['log_id']}:unknown-submitter"])
                        continue
                else:
                    person_id = referenced
                    matched = next((person for person in organization.persons
                                    if person.person_id == person_id), None)
                    if matched and matched.role == "基层学生" and f.get("submitter_name"):
                        person_id = ""  # 基层必须使用姓名栏
                if not person_id:
                    name = display_name(row["values"].get(f["submitter_ref"]))
                    issues[f"log:{row['log_id']}:unknown-submitter"] = (
                        f"日志 {row['log_id']} 的提交人「{name or '未知'}」"
                        "不在人员表，本条已跳过；请补录人员或改派")
                    row_day = submitted_at.astimezone(SHANGHAI).date()
                    if row_day in per_day:
                        per_day[row_day][1][f"log:{row['log_id']}:unknown-submitter"] = (
                            issues[f"log:{row['log_id']}:unknown-submitter"])
                    continue
                result.append(
                    WorkLog(
                        log_id=row["log_id"],
                        source_record_id=str(row["item"].get("record_id", "")) or None,
                        submitted_at=submitted_at,
                        person_id=person_id,
                        progress=scalar(row["values"].get(f["progress"])),
                        difficulties=scalar(row["values"].get(f["difficulties"])),
                        reflection=scalar(row["values"].get(f["reflection"])),
                        other=scalar(row["values"].get(f["other"])),
                        full_log=scalar(row["values"].get(f["full_log"])),
                        achievement_refs=(json.loads(scalar(row["values"].get(f["achievement_refs"])) or "[]")
                            if f.get("achievement_refs") else []),
                        resources=([LogResource.model_validate(item) for item in
                            json.loads(scalar(row["values"].get(f["resources"])) or "[]")]
                            if f.get("resources") else []),
                    )
                )
            # One log per person per day: the latest submission wins outright.
            by_day: dict[date, list[WorkLog]] = {}
            for log in result:
                by_day.setdefault(
                    log.submitted_at.astimezone(SHANGHAI).date(), []).append(log)
            for day, day_logs in by_day.items():
                day_issues = per_day[day][1]
                latest: dict[str, tuple[int, WorkLog]] = {}
                for order, log in enumerate(day_logs):
                    current = latest.get(log.person_id)
                    if current is None or (log.submitted_at, order) >= (
                            current[1].submitted_at, current[0]):
                        if current is not None:
                            day_issues[f"log:{current[1].log_id}:superseded"] = (
                                f"同一人同日多条日志，{current[1].log_id} 已被 "
                                f"{log.log_id}（最后一条）取代")
                        latest[log.person_id] = (order, log)
                    else:
                        day_issues[f"log:{log.log_id}:superseded"] = (
                            f"同一人同日多条日志，{log.log_id} 已被 "
                            f"{current[1].log_id}（最后一条）取代")
                per_day[day] = ([log for _, log in sorted(
                    latest.values(), key=lambda pair: pair[0])], day_issues)
            return per_day
        except FeishuReadError:
            raise
        except Exception as exc:
            raise FeishuReadError(
                f"Unable to load logs for {start_day}~{end_day}") from exc

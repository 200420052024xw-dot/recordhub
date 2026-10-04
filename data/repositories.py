from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from errors import FeishuReadError
from schema import (
    Department,
    Organization,
    Person,
    TableConfig,
    WorkLog,
)
from data import FileStateStore
from tool.feishu import BitableService, ContactService


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _fields(record: Mapping[str, Any]) -> dict[str, Any]:
    value = record.get("fields", {})
    return dict(value) if isinstance(value, Mapping) else {}


def _scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return _scalar(value[0]) if value else ""
    if isinstance(value, Mapping):
        for key in (
            "record_id",
            "id",
            "open_id",
            "user_id",
            "link",
            "text",
            "texts",
            "mobile",
            "name",
            "value",
        ):
            if key in value:
                return _scalar(value[key])
    return str(value)


def _display_name(value: Any) -> str:
    if isinstance(value, list):
        return _display_name(value[0]) if value else ""
    if isinstance(value, Mapping) and value.get("name"):
        return str(value["name"])
    return _scalar(value)


def _references(value: Any) -> list[str]:
    """Return every usable identifier exposed by a Feishu relation/person field."""
    if value is None:
        return []
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        return [text] if text else []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_references(item))
        return list(dict.fromkeys(result))
    if isinstance(value, Mapping):
        result = []
        for key in (
            "record_ids",
            "record_id",
            "id",
            "person_id",
            "open_id",
            "user_id",
            "text",
            "name",
            "value",
        ):
            if key in value:
                result.extend(_references(value[key]))
        return list(dict.fromkeys(result))
    return [str(value)]


def _alias_index(rows: Iterable[tuple[str, Iterable[str]]]) -> dict[str, str]:
    """Build an alias map while rejecting ambiguous duplicate display names."""
    aliases: dict[str, str | None] = {}
    for canonical_id, candidates in rows:
        if not canonical_id:
            continue
        for candidate in candidates:
            alias = str(candidate).strip()
            if not alias:
                continue
            if alias in aliases and aliases[alias] != canonical_id:
                aliases[alias] = None
            else:
                aliases[alias] = canonical_id
    return {alias: target for alias, target in aliases.items() if target}


def _resolve_reference(value: Any, aliases: Mapping[str, str]) -> str:
    for candidate in _references(value):
        if candidate in aliases:
            return aliases[candidate]
    return ""


def _boolean(value: Any, *, default: bool = True) -> bool:
    text = _scalar(value).strip().lower()
    if not text:
        return default
    return text in {"true", "1", "yes", "是", "有效", "启用"}


def _datetime(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=UTC)
    text = _scalar(value)
    if not text:
        raise ValueError("datetime field is empty")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=SHANGHAI)


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
                (record, _scalar(_fields(record).get(mobile_field)).strip())
                for record in person_records
            ]
            person_mobiles = [(record, mobile) for record, mobile in person_mobiles
                              if mobile]
            person_records = [record for record, _ in person_mobiles]
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
            person_records = [record for record, mobile in person_mobiles
                              if mobile in mobile_open_ids]
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
                return _scalar(values.get(person_fields["name"])).strip()
            return mobile_open_ids.get(
                _scalar(values.get(person_fields["mobile"])).strip(), ""
            )
        department_rows = []
        for item in department_records:
            values = _fields(item)
            department_id = _scalar(
                values.get(department_fields["department_id"])
            ).strip()
            department_rows.append((item, values, department_id))
        department_aliases = _alias_index(
            (
                department_id,
                (
                    department_id,
                    str(item.get("record_id", "")),
                    _scalar(values.get(department_fields["name"])),
                ),
            )
            for item, values, department_id in department_rows
        )

        person_rows = []
        for item in person_records:
            values = _fields(item)
            person_id = (_scalar(values.get(person_fields["person_id"])).strip()
                         or str(item.get("record_id", "")))
            person_rows.append((item, values, person_id))
        person_aliases = _alias_index(
            (
                person_id,
                (
                    person_id,
                    str(item.get("record_id", "")),
                    _display_name(values.get(person_fields["name"])),
                    person_open_id(item, values),
                ),
            )
            for item, values, person_id in person_rows
        )

        persons = []
        for item, values, person_id in person_rows:
            role = _scalar(values.get(person_fields["role"]))
            leader_id = _resolve_reference(
                values.get(person_fields["leader_ref"]), person_aliases
            )
            if not leader_id and role == "骨干学生":
                leader_id = _resolve_reference(
                    values.get(person_fields["minister_ref"]), person_aliases
                )
            persons.append(
                Person(
                    person_id=person_id,
                    name=_display_name(values.get(person_fields["name"])),
                    role=role,
                    department_id=_resolve_reference(
                        values.get(person_fields["department_ref"]),
                        department_aliases,
                    )
                    or None,
                    leader_id=leader_id or None,
                    open_id=person_open_id(item, values) or None,
                    source_record_id=str(item.get("record_id", "")) or None,
                    active=_boolean(values.get(person_fields.get("active"))),
                )
            )

        departments = []
        for item, values, department_id in department_rows:
            minister_id = _resolve_reference(
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
                    name=_scalar(values.get(department_fields["name"]))
                    or department_id,
                    minister_id=minister_id or None,
                    source_record_id=str(item.get("record_id", "")) or None,
                    active=_boolean(values.get(department_fields.get("active"))),
                )
            )
        return departments, persons

    @staticmethod
    def _find_anomalies(departments, persons) -> list[str]:
        anomalies: list[str] = []
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
        if not person.person_id or not person.name or not person.role:
            anomalies.append(f"人员记录缺少编号、姓名或角色: {person.name or '未知'}")
        if person.department_id and person.department_id not in department_ids:
            anomalies.append(f"人员 {person.person_id} 的部门不存在")
        if person.leader_id and person.leader_id not in person_ids:
            anomalies.append(f"人员 {person.person_id} 的直属上级不存在")
        if person.active and person.role in {"基层学生", "骨干学生"}:
            if not person.leader_id:
                anomalies.append(f"人员 {person.person_id} 没有直属上级")
            elif person.leader_id in person_ids:
                expected = "骨干学生" if person.role == "基层学生" else "部长"
                if person_by_id[person.leader_id].role != expected:
                    anomalies.append(
                        f"人员 {person.person_id} 的直属上级角色应为 {expected}"
                    )
        return anomalies


class OrganizationCache:
    def __init__(self, store: FileStateStore, repository: OrganizationRepository) -> None:
        self.store = store
        self.repository = repository

    def get(self) -> Organization:
        cached = self.store.load_cache("organization")
        if cached is not None:
            organization = Organization.model_validate(cached)
            if all(not person.open_id or person.open_id.startswith("ou_")
                   for person in organization.persons):
                return organization
        return self.refresh()

    def refresh(self) -> Organization:
        organization = self.repository.load()
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

    def get_logs_by_date(self, target_date: date,
                         organization: Organization | None = None) -> list[WorkLog]:
        start = datetime.combine(target_date, time.min, SHANGHAI)
        end = start + timedelta(days=1)
        try:
            organization = organization or self.organization_cache.get()
            person_aliases = _alias_index(
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
            records = self.bitable.list_records(
                self.table.table_id,
                field_names=list(self.table.fields.values()),
            )
            result: list[WorkLog] = []
            f = self.table.fields
            for item in records:
                values = _fields(item)
                submitted_at = _datetime(values.get(f["submitted_at"]))
                if not start <= submitted_at < end:
                    continue
                log_id = _scalar(values.get(f["log_id"])) or str(
                    item.get("record_id", "")
                )
                person_id = _resolve_reference(
                    values.get(f["submitter_ref"]), person_aliases
                )
                if not person_id:
                    raise ValueError(
                        f"日志 {log_id} 的提交人无法匹配到人员表中的人员编号"
                    )
                result.append(
                    WorkLog(
                        log_id=log_id,
                        source_record_id=str(item.get("record_id", "")) or None,
                        submitted_at=submitted_at,
                        person_id=person_id,
                        progress=_scalar(values.get(f["progress"])),
                        difficulties=_scalar(values.get(f["difficulties"])),
                        reflection=_scalar(values.get(f["reflection"])),
                        other=_scalar(values.get(f["other"])),
                        full_log=_scalar(values.get(f["full_log"])),
                    )
                )
            return result
        except Exception as exc:
            raise FeishuReadError(f"Unable to load logs for {target_date}") from exc



"""阶段/月度/周度产出的通用读取器：表驱动，只读已发布的最终表。

输出用的表（stage_report / monthly_department_report / team_analysis）都是 Workflow2
在发布阶段写入的定稿；材料准备阶段的草稿表（*_analysis）与确认表不对外提供。
表在配置里可选：缺失或为空一律静默跳过，不报错。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Literal

from schema import TableConfig
from schema.domain import Organization
from tool.bitable_fields import (
    SHANGHAI,
    alias_index,
    field_datetime,
    record_fields,
    resolve_reference,
    scalar,
)

from service.workbuddy_query.cache import TtlCache


@dataclass(frozen=True)
class StageTable:
    name: str
    scope: Literal["department", "team"]
    content: tuple[tuple[str, str], ...]
    owner_field: str | None = None
    date_field: str = "analysis_date"
    task_id_field: str = "task_id"


STAGE_TABLES: tuple[StageTable, ...] = (
    StageTable("stage_report", "department",
               (("阶段工作总结", "progress"),), owner_field="minister_ref"),
    StageTable("monthly_department_report", "department",
               (("降级—奖励—退出—提拔名单", "personnel_s05"),
                ("idea 清单", "ideas_s06"),
                ("学生例会重点人员", "meeting_s07")),
               owner_field="minister_ref"),
    StageTable("team_analysis", "team",
               (("公共技术", "technology_s08"),
                ("公共培训建议", "training_s09"))),
)


@dataclass(frozen=True)
class StageRecord:
    item_id: str
    date: date
    name: str
    department_id: str | None
    sections: tuple[dict, ...]


class StageReader:
    def __init__(self, bitable, tables: TableConfig, organization: Organization,
                 *, cache: TtlCache | None = None) -> None:
        self.bitable = bitable
        self.tables = tables
        self.organization = organization
        self.cache = cache
        self._people = organization.person_map()
        self._departments = organization.department_map()
        self._aliases = alias_index(
            (item.person_id,
             (item.person_id, item.name, item.open_id or "", item.source_record_id or ""))
            for item in organization.persons)

    def read(self, start: date, end: date, *, department_ids: set[str] | None,
             include_team: bool) -> list[StageRecord]:
        records: list[StageRecord] = []
        for descriptor in STAGE_TABLES:
            records.extend(self._read_table(
                descriptor, start, end, department_ids, include_team))
        records.sort(key=lambda record: (record.date, record.name))
        return records

    def _read_table(self, descriptor: StageTable, start: date, end: date,
                    department_ids: set[str] | None,
                    include_team: bool) -> list[StageRecord]:
        table = self.tables.tables.get(descriptor.name)
        if table is None or not table.table_id.strip():
            return []
        fields = table.fields
        records: list[StageRecord] = []
        for row in self._list_records(table.table_id):
            values = record_fields(row)
            day = self._day(values.get(fields.get(descriptor.date_field, "")))
            if day is None or not start <= day <= end:
                continue
            task_id = str(scalar(values.get(fields.get(descriptor.task_id_field, "")))).strip()
            is_team = descriptor.scope == "team" or task_id.endswith(":TEAM")
            if is_team:
                if not include_team:
                    continue
                name, department_id = "团队", None
            else:
                owner_id = resolve_reference(
                    values.get(fields.get(descriptor.owner_field or "", "")), self._aliases)
                owner = self._people.get(owner_id)
                if owner is None:
                    continue
                department_id = owner.department_id
                if department_ids is not None and department_id not in department_ids:
                    continue
                department = self._departments.get(department_id or "")
                name = department.name if department else (department_id or "未分配部门")
            sections = self._sections(values, fields, descriptor)
            record_id = str(row.get("record_id", ""))
            if not sections or not record_id:
                continue
            records.append(StageRecord(item_id=record_id, date=day, name=name,
                                       department_id=department_id,
                                       sections=tuple(sections)))
        return records

    @staticmethod
    def _sections(values: dict, fields: dict, descriptor: StageTable) -> list[dict]:
        sections: list[dict] = []
        for label, key in descriptor.content:
            field_name = fields.get(key)
            if not field_name:
                continue
            text, url = _link_parts(values.get(field_name))
            if not text and not url:
                continue
            section: dict = {"label": label}
            if text:
                section["text"] = text
            if url:
                section["url"] = url
            sections.append(section)
        return sections

    def _list_records(self, table_id: str) -> list[dict]:
        if self.cache is None:
            return self.bitable.list_records(table_id)
        return self.cache.get_or_load(
            ("stage", table_id), lambda: self.bitable.list_records(table_id))

    @staticmethod
    def _day(raw) -> date | None:
        if not raw:
            return None
        try:
            return field_datetime(raw).astimezone(SHANGHAI).date()
        except (ValueError, TypeError):
            return None


def _link_parts(value) -> tuple[str, str]:
    """超链接字段是 ``{text, link}``；scalar() 会先返回 link，故需单独取值。"""
    if isinstance(value, (list, tuple)) and value:
        value = value[0]
    if isinstance(value, Mapping):
        return (str(value.get("text") or "").strip(),
                str(value.get("link") or "").strip())
    return str(scalar(value)).strip(), ""

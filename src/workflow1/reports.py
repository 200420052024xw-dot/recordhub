"""Index Workflow1 documents in the existing five-column report table."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from tool.bitable_fields import field_datetime, record_fields, references, scalar
from data.store import FileStateStore
from schema import CloudObject, TableConfig
from workflow1.models import DailySnapshot
from tool.feishu import BitableService


class WorkflowReportRepository:
    def __init__(self, bitable: BitableService, config: TableConfig,
                 store: FileStateStore) -> None:
        self.bitable = bitable
        self.table = config.tables["reports"]
        self.details_table = config.tables.get("check_details")
        self.store = store

    def publish_details(self, snapshot: DailySnapshot,
                        objects: dict[str, CloudObject]) -> dict[str, str]:
        """Fill submission counts without writing the auto-number first field."""
        table = self.details_table
        if table is None or not table.table_id:
            return {}
        f = table.fields
        people = snapshot.organization.person_map()
        rows = self.bitable.list_records(table.table_id)
        issues: dict[str, str] = {}
        local_day = snapshot.created_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        for key in objects:
            parts = key.split(":", 2)
            scope = parts[1]
            if scope == "TEAM":
                person_id = snapshot.organization.team_leader_id
                members = set(snapshot.submission_status)
            elif scope == "DEPARTMENT":
                department_id = parts[2]
                department = snapshot.organization.department_map()[department_id]
                person_id = department.minister_id
                members = {pid for pid in snapshot.submission_status
                           if people[pid].department_id == department_id}
            elif scope == "MEMBERS":
                person_id = parts[2]
                members = {person_id} | {pid for pid in snapshot.submission_status
                    if people[pid].leader_id == person_id}
            else:
                continue
            if not person_id or not people[person_id].open_id:
                issues[f"detail:{key}:missing-open-id"] = "检查明细缺少报告人 OpenID"
                continue
            submitted = sum(snapshot.submission_status[pid].submitted for pid in members)
            fields = {f["reporter_ref"]: [{"id": people[person_id].open_id}],
                      f["role"]: people[person_id].role,
                      f["submitted"]: submitted,
                      f["missing"]: len(members) - submitted}
            mapped = self.store.get_external_record(snapshot.target_date, key + ":DETAIL")
            matches = [row for row in rows if
                people[person_id].open_id in references(record_fields(row).get(f["reporter_ref"]))
                and self._near_day(record_fields(row).get(f["reported_at"]), local_day)]
            if mapped:
                matches = [row for row in matches
                           if row.get("record_id") == mapped["record_id"]]
                if not matches:
                    raise ValueError(f"检查明细 {key} 的本地映射与飞书不一致")
            if len(matches) > 1:
                raise ValueError(f"检查明细 {key} 存在多条候选记录")
            if matches:
                record_id = str(matches[0]["record_id"])
                current = record_fields(matches[0])
                if any(str(scalar(current.get(f[name]))) != str(fields[f[name]])
                       for name in ("submitted", "missing")):
                    self.bitable.update_record(table.table_id, record_id,
                        {f["submitted"]: submitted, f["missing"]: len(members) - submitted})
            else:
                fields[f["reported_at"]] = int(datetime.now().timestamp() * 1000)
                created = self.bitable.create_record(table.table_id, fields)
                record_id = str(created.get("record_id", ""))
                if not record_id:
                    raise ValueError(f"检查明细 {key} 创建后缺少记录 ID")
                rows.append({"record_id": record_id, "fields": fields})
            if not mapped:
                self.store.map_external_record(snapshot.target_date, key + ":DETAIL",
                                               "check_details", record_id)
        return issues

    @staticmethod
    def _near_day(value, day: date) -> bool:
        try:
            return abs((field_datetime(value).astimezone(ZoneInfo("Asia/Shanghai")).date() - day).days) <= 1
        except (ValueError, TypeError):
            return False

    def publish(self, snapshot: DailySnapshot,
                objects: dict[str, CloudObject]) -> dict[str, str]:
        """Index documents in the report table; returns issue entries to log."""
        issues: dict[str, str] = {}
        if not objects:
            return issues
        f = self.table.fields
        url_type = None
        if "document_url" in f:
            url_fields = [field for field in self.bitable.list_fields(self.table.table_id)
                          if field.get("field_name") == f["document_url"]]
            if len(url_fields) != 1:
                raise ValueError(f"报告表字段不存在或重名：{f['document_url']}")
            url_type = url_fields[0].get("type")
            if url_type not in (1, 15):
                raise ValueError(f"报告表链接字段类型不支持：{url_type}")
        existing = self.bitable.list_records(self.table.table_id)
        urls: dict[str, str] = {}
        for record in existing:
            fields = record_fields(record)
            url = (scalar(fields.get(f["document_url"])) if url_type else
                   scalar(fields.get(f["title"])))
            record_id = str(record.get("record_id", ""))
            if url in urls and urls[url] != record_id:
                raise ValueError(f"报告表中存在重复文档链接：{url}")
            if url:
                urls[url] = record_id
        people = snapshot.organization.person_map()
        departments = snapshot.organization.department_map()
        creates: list[tuple[str, dict]] = []
        for key, item in objects.items():
            local = self.store.get_external_record(snapshot.target_date, key)
            matched = next((record_id for url, record_id in urls.items()
                            if item.url in url), None)
            if local or matched:
                if not local:
                    self.store.map_external_record(
                        snapshot.target_date, key, "reports", matched)
                continue
            parts = key.split(":", 2)
            scope = parts[1]
            scope_id = parts[2] if len(parts) > 2 else "TEAM"
            if scope == "TEAM":
                person_id = snapshot.organization.team_leader_id
                title = f"{snapshot.target_date:%m-%d} 团队日志"
            elif scope == "DEPARTMENT":
                department = departments[scope_id]
                person_id = department.minister_id
                title = f"{snapshot.target_date:%m-%d} {department.name}日志"
            else:
                person_id = scope_id
                title = (f"{snapshot.target_date:%m-%d} "
                         f"{people[scope_id].name}同学小组日志")
            if not person_id:
                raise ValueError(f"报告 {key} 缺少报告人")
            if not people[person_id].open_id:
                issues[f"report:{key}:missing-open-id"] = (
                    f"报告「{key}」的报告人「{people[person_id].name}」缺少 OpenID，"
                    "索引行未写入，请补录 OpenID 后重跑")
                continue
            fields = {
                f["reporter_ref"]: [{"id": people[person_id].open_id}],
                f["role"]: people[person_id].role,
                f["reported_at"]: int(datetime.now().timestamp() * 1000),
            }
            if url_type:
                fields[f["document_url"]] = (
                    {"text": title, "link": item.url}
                    if url_type == 15 else item.url
                )
            else:
                fields[f["title"]] = f"{title}\n{item.url}"
            creates.append((key, fields))
        for offset in range(0, len(creates), 500):
            chunk = creates[offset:offset + 500]
            created = self.bitable.batch_create(
                self.table.table_id, [fields for _, fields in chunk])
            if len(created) != len(chunk):
                raise ValueError("飞书批量创建报告索引返回数量不一致")
            for (key, _), record in zip(chunk, created, strict=True):
                record_id = str(record.get("record_id", ""))
                if not record_id:
                    raise ValueError(f"报告 {key} 创建后没有记录 ID")
                self.store.map_external_record(
                    snapshot.target_date, key, "reports", record_id)
        return issues

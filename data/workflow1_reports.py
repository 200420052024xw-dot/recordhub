"""Index Workflow1 documents in the existing five-column report table."""

from __future__ import annotations

from datetime import date, datetime

from data.repositories import _fields, _scalar
from data.store import FileStateStore
from schema import CloudObject, DailySnapshot, TableConfig
from tool.feishu import BitableService


class WorkflowReportRepository:
    def __init__(self, bitable: BitableService, config: TableConfig,
                 store: FileStateStore) -> None:
        self.bitable = bitable
        self.table = config.tables["reports"]
        self.store = store

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
            fields = _fields(record)
            url = (_scalar(fields.get(f["document_url"])) if url_type else
                   _scalar(fields.get(f["title"])))
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
                f["title"]: title if url_type else f"{title}\n{item.url}",
                f["reporter_ref"]: [{"id": people[person_id].open_id}],
                f["role"]: people[person_id].role,
                f["reported_at"]: int(datetime.now().timestamp() * 1000),
            }
            if url_type:
                fields[f["document_url"]] = (
                    {"text": title, "link": item.url}
                    if url_type == 15 else item.url
                )
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

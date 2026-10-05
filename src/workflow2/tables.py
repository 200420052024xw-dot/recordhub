"""Idempotent Bitable writes using the dedicated task-id text field."""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from schema import TableConfig
from tool.bitable_fields import record_fields, scalar
from tool.feishu import BitableService


def date_millis(value: date) -> int:
    return int(datetime.combine(value, time(), ZoneInfo("Asia/Shanghai")).timestamp() * 1000)


class Workflow2Tables:
    def __init__(self, bitable: BitableService, config: TableConfig):
        self.bitable, self.config = bitable, config

    def table(self, name: str):
        table = self.config.tables.get(name)
        if table is None or not table.table_id.strip():
            raise ValueError(f"Workflow2 缺少飞书表 ID：{name}")
        return table

    def require(self, names: list[str]) -> None:
        for name in names:
            table = self.table(name)
            actual = {item.get("field_name"): item.get("type")
                      for item in self.bitable.list_fields(table.table_id)}
            if actual.get(table.fields["task_id"]) != 1:
                raise ValueError(f"{name} 的任务编号必须是可写文本列")
            for key, column in table.fields.items():
                if key == "auto_number":
                    continue
                if column not in actual:
                    raise ValueError(f"{name} 缺少字段：{column}")

    def upsert(self, name: str, task_id: str, values: dict[str, object]) -> str:
        table = self.table(name)
        f = table.fields
        rows = [row for row in self.bitable.list_records(table.table_id)
                if scalar(record_fields(row).get(f["task_id"])) == task_id]
        if len(rows) > 1:
            raise ValueError(f"{name} 中任务 {task_id} 出现重复记录")
        fields = {f[key]: value for key, value in values.items()}
        fields[f["task_id"]] = task_id
        if rows:
            row = rows[0]
            current = record_fields(row)
            changed = {key: value for key, value in fields.items()
                       if current.get(key) != value}
            if changed:
                self.bitable.update_record(table.table_id, str(row["record_id"]), changed)
            return str(row["record_id"])
        row = self.bitable.create_record(table.table_id, fields)
        if not row.get("record_id"):
            raise ValueError(f"{name} 创建结果缺少 record_id")
        return str(row["record_id"])

"""Adapters for existing Bitable columns: JSON cells and stable title markers."""

from __future__ import annotations

import json
from datetime import datetime, time
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from tool.bitable_fields import record_fields as _fields, references as _references, scalar as _scalar
from schema import Organization, TableConfig
from analysis.models import AnalysisRun
from skills import SkillConfig
from tool.feishu import BitableService


ANALYSIS_FIELDS = {"S04": "progress_s04", "S05": "personnel_s05", "S06": "ideas_s06",
                   "S07": "meeting_focus_s07", "S08": "technology_s08", "S09": "training_s09"}


class SkillConfigRepository:
    def __init__(self, bitable: BitableService, tables: TableConfig,
                 local_path: str, team_department_id: str):
        self.bitable, self.tables = bitable, tables
        self.local_path = Path(local_path)
        self.team_department_id = team_department_id

    def load(self, organization: Organization) -> list[SkillConfig]:
        configs = []
        if self.local_path.exists():
            raw = json.loads(self.local_path.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, list):
                raise ValueError("本地 Skill 配置必须是 JSON 数组")
            configs.extend(SkillConfig.model_validate(item) for item in raw)
        table = self.tables.tables.get("prompts")
        if table and table.table_id.strip():
            f = table.fields
            if not {"config_id", "template", "user_ref"} <= set(f):
                raise ValueError("飞书 Skill 配置表缺少编号、配置内容或使用人映射")
            for record in self.bitable.list_records(table.table_id):
                fields = _fields(record)
                text = _scalar(fields.get(f["template"]))
                if not text.strip():
                    continue
                try:
                    raw = json.loads(text)
                    parsed = [SkillConfig.model_validate(item) for item in (raw if isinstance(raw, list) else [raw])]
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"Skill 配置记录 {record.get('record_id')} 需使用结构化 JSON：{exc}") from exc
                references = set(_references(fields.get(f["user_ref"])))
                for config in parsed:
                    if not config.enabled:
                        continue
                    if config.user_id:
                        person = organization.person_map().get(config.user_id)
                        aliases = {config.user_id}
                        if person:
                            aliases.update(filter(None, (person.open_id, person.source_record_id)))
                        if not references or not references & aliases:
                            raise ValueError(f"配置 {config.config_id} 的使用人与飞书人员字段不一致")
                    elif references:
                        raise ValueError(f"部门默认配置 {config.config_id} 不应绑定具体使用人")
                configs.extend(parsed)
        people = organization.person_map()
        departments = organization.department_map()
        for config in configs:
            if not config.enabled:
                continue
            if config.department_id not in departments and config.department_id != self.team_department_id:
                raise ValueError(f"Skill 配置 {config.config_id} 归属部门不存在")
            if config.user_id and (config.user_id not in people or not people[config.user_id].active):
                raise ValueError(f"Skill 配置 {config.config_id} 使用人无效")
        return configs


class AnalysisRepository:
    def __init__(self, bitable: BitableService, tables: TableConfig):
        self.bitable, self.tables = bitable, tables

    def layout(self, run: AnalysisRun):
        name = "department_analysis" if run.scope_type == "DEPARTMENT" else "team_analysis"
        table = self.tables.tables.get(name)
        if not table or not table.table_id.strip():
            raise ValueError(f"周期分析缺少飞书表配置：{name}")
        if not table.fields.get("title"):
            raise ValueError(f"分析表 {name} 缺少 title 映射")
        target = table.fields.get(ANALYSIS_FIELDS[run.request.skill_code.value])
        # An optional generic column can hold any Skill's full JSON envelope.
        target = target or table.fields.get("result_json") or table.fields["title"]
        actual = {field["field_name"]: field.get("type")
                  for field in self.bitable.list_fields(table.table_id)}
        for field in {table.fields["title"], target}:
            if actual.get(field) != 1:
                raise ValueError(f"分析字段 {field} 必须存在且为文本字段")
        for key, expected in (("minister_ref", 11), ("analysis_date", 5)):
            if key in table.fields and actual.get(table.fields[key]) != expected:
                raise ValueError(f"分析字段 {table.fields[key]} 的类型不符合 {key} 映射")
        return table, target

    @staticmethod
    def marker(run: AnalysisRun) -> str:
        return f"RecordHub:{run.request.skill_code.value}:{run.run_id}"

    def _find(self, run: AnalysisRun, table):
        marker, title = self.marker(run), table.fields["title"]
        matches = [record for record in self.bitable.list_records(table.table_id)
                   if _scalar(_fields(record).get(title)).split("\n", 1)[0] == marker]
        if len(matches) > 1:
            raise ValueError(f"分析任务 {run.run_id} 在飞书中重复，请人工核对")
        if run.external_record_id:
            if not matches or matches[0].get("record_id") != run.external_record_id:
                raise ValueError("分析记录映射与飞书记录不一致")
        return matches[0] if matches else None

    def _json(self, record, run: AnalysisRun, table, target) -> dict:
        text = _scalar(_fields(record).get(target))
        if target == table.fields["title"]:
            if not text.startswith(self.marker(run) + "\n"):
                raise ValueError("分析记录缺少稳定业务标识")
            text = text.split("\n", 1)[1]
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("分析结果必须是 JSON 对象")
        return value

    @staticmethod
    def _check_metadata(value: dict, result: dict) -> None:
        for key in ("result_id", "run_id", "skill_code", "config_id", "config_department_id",
                    "config_version", "based_on_version", "user_id", "scope", "source_record_ids", "generated_at", "material_statistics"):
            if value.get(key) != result.get(key):
                raise ValueError(f"飞书分析记录的 {key} 被改变，请核对来源")

    def publish(self, run: AnalysisRun, result: dict, *, organization: Organization,
                confirmation: bool = False) -> str:
        table, target = self.layout(run)
        existing = self._find(run, table)
        fields = {table.fields["title"]: self.marker(run)}
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        fields[target] = (self.marker(run) + "\n" + text if target == table.fields["title"] else text)
        if existing:
            previous = self._json(existing, run, table, target)
            self._check_metadata(previous, result)
            record_id = str(existing.get("record_id", ""))
            # Keep human edits to pending content. They are validated at confirmation.
            if confirmation and previous != result:
                self.bitable.update_record(table.table_id, record_id, fields)
            return record_id
        if confirmation:
            raise ValueError("无法确认：飞书分析记录不存在")
        person = organization.person_map()[run.request.user_id]
        if "minister_ref" in table.fields:
            if not person.open_id:
                raise ValueError("分析确认人缺少飞书 OpenID")
            fields[table.fields["minister_ref"]] = [{"id": person.open_id}]
        if "analysis_date" in table.fields:
            fields[table.fields["analysis_date"]] = int(datetime.combine(
                run.request.end_date, time(), ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
        created = self.bitable.create_record(table.table_id, fields)
        record_id = str(created.get("record_id", ""))
        if not record_id:
            raise ValueError("飞书创建分析结果未返回记录 ID")
        return record_id

    def read(self, run: AnalysisRun) -> dict:
        table, target = self.layout(run)
        record = self._find(run, table)
        if record is None:
            raise ValueError("飞书分析记录不存在")
        value = self._json(record, run, table, target)
        self._check_metadata(value, run.result)
        return value

    def url(self, run: AnalysisRun) -> str:
        table_name = "department_analysis" if run.scope_type == "DEPARTMENT" else "team_analysis"
        table = self.tables.tables[table_name]
        return f"https://feishu.cn/base/{self.bitable.app_token}?" + urlencode(
            {"table": table.table_id, "record": run.external_record_id or ""})

"""Adapters for existing Bitable columns: JSON cells and stable title markers."""

from __future__ import annotations

import json
from datetime import datetime, time
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from tool.bitable_fields import record_fields as _fields, references as _references, scalar as _scalar
from schema import Organization, TableConfig
from workflow2.skills import SkillConfig
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

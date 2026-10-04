"""Resolve department/person versions and dispatch standalone Python Skills."""

from __future__ import annotations

from collections.abc import Iterable

from llm import PromptService
from skills.models import SkillCode, SkillConfig, SkillInput, SkillResult
from skills.s01 import S01Skill
from skills.s02 import S02Skill
from skills.s03 import S03Skill
from skills.s04 import S04Skill
from skills.s05 import S05Skill
from skills.s06 import S06Skill
from skills.s07 import S07Skill
from skills.s08 import S08Skill
from skills.s09 import S09Skill


SKILLS = {skill.code: skill for skill in (
    S01Skill(), S02Skill(), S03Skill(), S04Skill(), S05Skill(),
    S06Skill(), S07Skill(), S08Skill(), S09Skill(),
)}


class SkillRunner:
    def __init__(self, service: PromptService,
                 configs: Iterable[SkillConfig] = ()) -> None:
        self.service = service
        # Freeze caller-owned configs so later mutations cannot change resolution.
        self.configs = tuple(SkillConfig.model_validate(item.model_dump()) for item in configs)

    @staticmethod
    def output_schema(code: SkillCode | str) -> dict:
        return SKILLS[SkillCode(code)].output_model.model_json_schema()

    def resolve_config(self, code: SkillCode | str, *, department_id: str,
                       user_id: str) -> SkillConfig:
        code = SkillCode(code)
        department_id, user_id = department_id.strip(), user_id.strip()
        if not department_id or not user_id:
            raise ValueError("配置归属部门和使用人编号不能为空")
        matches = [item for item in self.configs
                   if item.enabled and item.skill_code == code
                   and item.department_id == department_id
                   and item.user_id in {None, user_id}]
        personal = [item for item in matches if item.user_id == user_id]
        department = [item for item in matches if item.user_id is None]
        selected = personal or department
        if len(selected) > 1:
            raise ValueError("同一优先级存在多个 Skill 版本，请由程序指定唯一生效版本")
        if selected:
            return selected[0].model_copy(deep=True)
        return SkillConfig(config_id=f"builtin:{code.value}", skill_code=code,
            department_id=department_id, version=SKILLS[code].version,
            based_on_version=SKILLS[code].version)

    def run(self, code: SkillCode | str, data: SkillInput, *,
            department_id: str, user_id: str) -> SkillResult:
        code = SkillCode(code)
        config = self.resolve_config(code, department_id=department_id, user_id=user_id)
        return SKILLS[code].run(self.service, data, config, user_id=user_id.strip())

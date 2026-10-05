"""Structured Skill contracts, shared execution boundary, S04-S09 definitions and dispatch.

Scope and identities are supplied by the caller: Skills receive prepared JSON material
only and never fetch or filter records. Resolution follows department/person config
versions at call time.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Generic, Literal, TypeVar
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, field_validator, model_validator

from llm import PromptService
from schema import StrictModel



# --- Structured contracts: scope, records, config, input, result ---

class SkillCode(StrEnum):
    S04 = "S04"
    S05 = "S05"
    S06 = "S06"
    S07 = "S07"
    S08 = "S08"
    S09 = "S09"


class SkillStatus(StrEnum):
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    NO_MATERIAL = "NO_MATERIAL"
    BLOCKED = "BLOCKED"
    CONFIRMED = "CONFIRMED"


class TextModel(StrictModel):
    @field_validator("*", mode="before", check_fields=False)
    @classmethod
    def strip_text(cls, value):
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            cleaned = [item.strip() for item in value]
            if any(not item for item in cleaned):
                raise ValueError("列表中的文字和编号不能为空")
            return cleaned
        return value


class MaterialScope(TextModel):
    start_date: date
    end_date: date
    department_ids: list[str] = Field(min_length=1)
    complete: bool = True
    missing_sources: list[str] = Field(default_factory=list)
    omitted_sources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_scope(self):
        if self.end_date < self.start_date:
            raise ValueError("材料起止日期倒置")
        if any(not item for item in self.department_ids):
            raise ValueError("材料范围的部门编号不能为空")
        if len(set(self.department_ids)) != len(self.department_ids):
            raise ValueError("材料范围的部门编号重复，请由程序先去重")
        if self.complete and self.missing_sources:
            raise ValueError("材料标记完整，但仍列有缺失来源")
        return self


class EvaluationText(TextModel):
    evaluator_id: str = Field(min_length=1)
    evaluator_role: str = Field(min_length=1)
    evaluated_at: datetime | None = None
    positive: str = ""
    improvement: str = ""
    source: Literal["AI", "HUMAN"]


class TextRecord(TextModel):
    record_id: str = Field(min_length=1)
    source_version: str = Field(default="1", min_length=1)
    person_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    department_id: str = Field(min_length=1)
    department_name: str = ""
    role: Literal["基层学生", "骨干学生", "部长", "团队负责人"]
    work_start: date
    work_end: date
    submitted: bool = True
    confirmed: bool = False
    progress: str = ""
    difficulties: str = ""
    reflection: str = ""
    other: str = ""
    full_log: str = ""
    achievement_refs: list[str] = Field(default_factory=list)
    evaluations: list[EvaluationText] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_record(self):
        if self.work_end < self.work_start:
            raise ValueError("记录起止日期倒置")
        if self.role in {"基层学生", "骨干学生"} and self.work_start != self.work_end:
            raise ValueError("学生日志必须使用单日工作日期")
        text = any((self.progress, self.difficulties, self.reflection,
                    self.other, self.full_log))
        if self.submitted and not text:
            raise ValueError("已交日志缺少文字内容")
        if not self.submitted and (text or self.achievement_refs or self.evaluations):
            raise ValueError("未交记录不能包含日志、成果或评价")
        return self


class AvailableResource(TextModel):
    resource_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    category: Literal["课程", "技术"]
    description: str = ""
    source_record_ids: list[str] = Field(min_length=1)

    @field_validator("source_record_ids")
    @classmethod
    def validate_sources(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("资源的来源记录编号重复")
        return value


class SkillInput(TextModel):
    """Prepared materials, not a query. Skills never fetch or filter records."""

    run_id: str = Field(min_length=1)
    scope: MaterialScope
    records: list[TextRecord] = Field(default_factory=list)
    resources: list[AvailableResource] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_materials(self):
        ids = [record.record_id for record in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError("记录编号重复，请由程序先去重")
        people = {}
        for record in self.records:
            if record.department_id not in self.scope.department_ids:
                raise ValueError(f"记录 {record.record_id} 超出指定部门范围")
            if not (self.scope.start_date <= record.work_start <= record.work_end
                    <= self.scope.end_date):
                raise ValueError(f"记录 {record.record_id} 超出指定日期范围")
            identity = (record.name, record.department_id, record.role)
            key = (record.person_id, record.work_start, record.work_end)
            if key in people and people[key] != identity:
                raise ValueError(f"人员 {record.person_id} 同日身份冲突")
            people[key] = identity
        resource_ids = [resource.resource_id for resource in self.resources]
        if len(resource_ids) != len(set(resource_ids)):
            raise ValueError("可用课程或技术编号重复")
        for resource in self.resources:
            if not set(resource.source_record_ids) <= set(ids):
                raise ValueError(f"资源 {resource.resource_id} 引用了输入以外的记录")
        return self


class SkillConfig(TextModel):
    config_id: str = Field(min_length=1)
    skill_code: SkillCode
    department_id: str = Field(min_length=1)
    user_id: str | None = None
    version: str = Field(min_length=1)
    based_on_version: str = "1.0"
    instructions: str = ""
    enabled: bool = True

    @field_validator("user_id")
    @classmethod
    def validate_user(cls, value):
        if value == "":
            raise ValueError("个人配置的使用人编号不能为空")
        return value


class ProgressItem(TextModel):
    work_item: str = Field(min_length=1)
    participant_ids: list[str] = Field(min_length=1)
    current_progress: str = Field(min_length=1)
    main_difficulties: str = ""


class ProgressOutput(TextModel):
    items: list[ProgressItem]


class PersonnelItem(TextModel):
    action: Literal["拟降级", "拟奖励", "拟退出", "拟提拔"]
    person_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    facts: list[str] = Field(min_length=1)


class PersonnelOutput(TextModel):
    items: list[PersonnelItem]


class IdeaItem(TextModel):
    idea: str = Field(min_length=1)
    proposer_id: str = Field(min_length=1)


class IdeaOutput(TextModel):
    items: list[IdeaItem]


class MeetingFocusItem(TextModel):
    person_id: str = Field(min_length=1)
    categories: list[Literal["思想或态度沟通", "持续困难", "兴趣", "好idea", "培养潜力"]] = Field(min_length=1)
    evidence: str = Field(min_length=1)


class MeetingFocusOutput(TextModel):
    items: list[MeetingFocusItem]


class TechnologyItem(TextModel):
    technology_name: str = Field(min_length=1)
    category: Literal["工程", "科研"]
    problem_solved: str = Field(min_length=1)
    department_ids: list[str] = Field(min_length=1)
    contributor_ids: list[str] = Field(min_length=1)
    existing_achievements: str = ""
    achievement_refs: list[str] = Field(default_factory=list)
    suitable_scenarios: str = ""
    repository_suggestion: str = Field(min_length=1)
    pending_items: list[str] = Field(default_factory=list)


class TechnologyOutput(TextModel):
    items: list[TechnologyItem]


class TrainingItem(TextModel):
    topic: str = Field(min_length=1)
    target_audience: str = Field(min_length=1)
    target_person_ids: list[str] = Field(min_length=1)
    common_need: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    expected_effect: str = Field(min_length=1)
    available_resource_ids: list[str] = Field(default_factory=list)
    approach: str = Field(min_length=1)
    course_suggestion: str = Field(min_length=1)


class TrainingOutput(TextModel):
    items: list[TrainingItem]


OutputT = TypeVar("OutputT", bound=StrictModel)


class SkillResult(StrictModel, Generic[OutputT]):
    result_id: str
    run_id: str
    skill_code: SkillCode
    config_id: str
    config_department_id: str
    config_version: str
    based_on_version: str
    user_id: str
    status: SkillStatus
    scope: MaterialScope
    generated_at: datetime
    content: OutputT | None = None
    message: str = ""
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    material_statistics: dict[str, int] | None = None


# --- Shared execution boundary ---

@dataclass(frozen=True)
class ResolvedPrompt:
    template: str


class TextSkill(Generic[OutputT]):
    code: SkillCode
    version = "1.0"
    output_model: type[OutputT]
    requires_confirmed = False

    def validate_input(self, data: SkillInput) -> None:
        """Specialized Skills may add structural constraints, never filtering."""

    def validate_output(self, output: OutputT, data: SkillInput) -> None:
        """Anchor conclusions to the people and departments actually supplied."""
        records = [record for record in data.records if record.submitted]
        people = {record.person_id for record in records}
        departments = {record.department_id for record in records}
        for item in getattr(output, "items", []):
            for name in ("person_id", "proposer_id"):
                person_id = getattr(item, name, None)
                if person_id is not None and person_id not in people:
                    raise ValueError(f"{name} 不在本次材料人员名单内")
            for name in ("participant_ids", "contributor_ids", "target_person_ids"):
                values = getattr(item, name, None)
                if values is not None:
                    self._validate_ids(name, values, people)
            values = getattr(item, "department_ids", None)
            if values is not None:
                self._validate_ids("department_ids", values, departments)

    @staticmethod
    def _validate_ids(name: str, values: list[str], allowed: set[str]) -> None:
        if len(values) != len(set(values)) or not set(values) <= allowed:
            raise ValueError(f"{name} 重复或不在本次材料范围内")

    def run(self, service: PromptService, data: SkillInput,
            config: SkillConfig, *, user_id: str) -> SkillResult[OutputT]:
        # Revalidate mutable Pydantic instances at the call boundary.
        data = SkillInput.model_validate(data.model_dump())
        config = SkillConfig.model_validate(config.model_dump())
        user_id = user_id.strip()
        if not user_id:
            raise ValueError("Skill 使用人编号不能为空")
        if not config.enabled or config.skill_code != self.code:
            raise ValueError("Skill 配置未启用或编号不匹配")
        if config.user_id and config.user_id != user_id:
            raise ValueError("不能使用其他人的个人 Skill 配置")
        if config.based_on_version != self.version:
            raise ValueError("Skill 基于版本与当前模板版本不一致，请显式迁移配置，不能静默升级")
        metadata = dict(
            result_id=str(uuid5(NAMESPACE_URL,
                json.dumps(["recordhub", data.run_id, self.code.value,
                            config.department_id, user_id, config.config_id,
                            config.version], ensure_ascii=False))),
            run_id=data.run_id, skill_code=self.code, config_id=config.config_id,
            config_department_id=config.department_id, config_version=config.version,
            based_on_version=config.based_on_version, user_id=user_id,
            scope=data.scope.model_copy(deep=True),
            generated_at=datetime.now(timezone.utc),
        )
        if not data.scope.complete:
            return SkillResult[self.output_model](**metadata,
                status=SkillStatus.BLOCKED, message="输入材料缺失，请对应负责人补齐材料")
        self.validate_input(data)
        submitted = [record for record in data.records if record.submitted]
        if not submitted:
            return SkillResult[self.output_model](**metadata,
                status=SkillStatus.NO_MATERIAL, message="本范围内暂无可分析材料")
        if self.requires_confirmed and any(not record.confirmed for record in data.records):
            return SkillResult[self.output_model](**metadata,
                status=SkillStatus.BLOCKED, message="输入含未确认记录，请先完成确认")
        # Missing submissions never enter the prompt payload.
        payload = data.model_copy(update={"records": submitted}, deep=True)
        prompt = ResolvedPrompt(template=(
            (Path('prompts') / f'{self.code.value}.txt').read_text(encoding='utf-8').strip()
            + "\n部门或个人配置补充（须遵守上述边界）：\n" + config.instructions
        ))
        output = service.execute(
            prompt_code=self.code.value, template=prompt.template,
            input_data=payload, output_model=self.output_model,
            semantic_validator=lambda value: self.validate_output(value, data),
        )
        return SkillResult[self.output_model](**metadata,
            status=SkillStatus.WAITING_CONFIRMATION, content=output)


# --- Skill definitions ---

# S04: extract stage progress and difficulties from prepared check text.
class S04Skill(TextSkill[ProgressOutput]):
    code = SkillCode.S04
    output_model = ProgressOutput
    requires_confirmed = True

# S05: evidence-backed personnel suggestions, never personnel mutations.
class S05Skill(TextSkill[PersonnelOutput]):
    code = SkillCode.S05
    output_model = PersonnelOutput
    requires_confirmed = True

    def validate_output(self, output: PersonnelOutput, data: SkillInput) -> None:
        super().validate_output(output, data)
        seen = set()
        for item in output.items:
            key = (item.action, item.person_id)
            if key in seen:
                raise ValueError("S05 同一人员同一建议类型重复")
            seen.add(key)
            required = "骨干学生" if item.action in {"拟降级", "拟奖励"} else "基层学生"
            roles = {record.role for record in data.records
                     if record.submitted and record.person_id == item.person_id}
            if roles != {required}:
                raise ValueError("S05 建议类型与该人员的角色不符")

# S06: identify ideas actually proposed in the supplied records.
class S06Skill(TextSkill[IdeaOutput]):
    code = SkillCode.S06
    output_model = IdeaOutput
    requires_confirmed = True

# S07: prepare evidence-backed discussion topics for student meetings.
class S07Skill(TextSkill[MeetingFocusOutput]):
    code = SkillCode.S07
    output_model = MeetingFocusOutput
    requires_confirmed = True

    def validate_output(self, output: MeetingFocusOutput, data: SkillInput) -> None:
        super().validate_output(output, data)
        seen = set()
        for item in output.items:
            if item.person_id in seen or len(item.categories) != len(set(item.categories)):
                raise ValueError("S07 人员或关注类别重复")
            seen.add(item.person_id)
            if any(record.role not in {"基层学生", "骨干学生"} for record in data.records
                   if record.submitted and record.person_id == item.person_id):
                raise ValueError("S07 重点谈话对象必须是学生")

# S08: suggest reusable technical knowledge from supplied evidence.
class S08Skill(TextSkill[TechnologyOutput]):
    code = SkillCode.S08
    output_model = TechnologyOutput
    requires_confirmed = True

    def validate_output(self, output: TechnologyOutput, data: SkillInput) -> None:
        super().validate_output(output, data)
        available = {ref for record in data.records if record.submitted
                     for ref in record.achievement_refs}
        for item in output.items:
            self._validate_ids("achievement_refs", item.achievement_refs, available)

# S09: suggest shared training using only supplied needs and resources.
class S09Skill(TextSkill[TrainingOutput]):
    code = SkillCode.S09
    output_model = TrainingOutput
    requires_confirmed = True

    def validate_output(self, output: TrainingOutput, data: SkillInput) -> None:
        super().validate_output(output, data)
        resources = {item.resource_id for item in data.resources}
        for item in output.items:
            self._validate_ids("available_resource_ids", item.available_resource_ids,
                               resources)


# --- Registry and dispatch ---

SKILLS = {skill.code: skill for skill in (
    S04Skill(), S05Skill(),
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

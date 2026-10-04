"""Structured Skill contracts. Scope and identities are supplied by the caller."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Generic, Literal, TypeVar

from pydantic import Field, field_validator, model_validator

from schema import StrictModel


class SkillCode(StrEnum):
    S01 = "S01"
    S02 = "S02"
    S03 = "S03"
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


class LogTextOutput(TextModel):
    progress: str = ""
    difficulties: str = ""
    reflection: str = ""
    other: str = ""

    @model_validator(mode="after")
    def validate_text(self):
        if not any((self.progress, self.difficulties, self.reflection, self.other)):
            raise ValueError("整理后的日志不能全部为空")
        return self


class EvaluationSuggestion(TextModel):
    record_id: str = Field(min_length=1)
    positive: str = Field(min_length=1)
    improvement: str = Field(min_length=1)


class CheckOutput(TextModel):
    evaluations: list[EvaluationSuggestion]
    summary: str = Field(min_length=1)


class SummaryOutput(TextModel):
    summary: str = Field(min_length=1)


class CitedItem(TextModel):
    source_record_ids: list[str] = Field(min_length=1)

    @field_validator("source_record_ids")
    @classmethod
    def validate_sources(cls, value):
        if any(not item for item in value) or len(set(value)) != len(value):
            raise ValueError("来源记录编号必须非空且不重复")
        return value


class ProgressItem(CitedItem):
    work_item: str = Field(min_length=1)
    department_ids: list[str] = Field(min_length=1)
    participant_ids: list[str] = Field(min_length=1)
    current_progress: str = Field(min_length=1)
    main_difficulties: str = ""


class ProgressOutput(TextModel):
    items: list[ProgressItem]


class PersonnelItem(CitedItem):
    action: Literal["拟降级", "拟奖励", "拟退出", "拟提拔"]
    person_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    facts: list[str] = Field(min_length=1)


class PersonnelOutput(TextModel):
    items: list[PersonnelItem]


class IdeaItem(CitedItem):
    idea: str = Field(min_length=1)
    proposer_id: str = Field(min_length=1)


class IdeaOutput(TextModel):
    items: list[IdeaItem]


class MeetingFocusItem(CitedItem):
    person_id: str = Field(min_length=1)
    categories: list[Literal["思想或态度沟通", "持续困难", "兴趣", "好idea", "培养潜力"]] = Field(min_length=1)
    evidence: str = Field(min_length=1)


class MeetingFocusOutput(TextModel):
    items: list[MeetingFocusItem]


class TechnologyItem(CitedItem):
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


class TrainingItem(CitedItem):
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
    source_record_ids: list[str]
    generated_at: datetime
    content: OutputT | None = None
    message: str = ""
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    material_statistics: dict[str, int] | None = None

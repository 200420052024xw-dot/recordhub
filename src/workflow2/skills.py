"""Structured Skill contracts, shared execution boundary, S04-S09 definitions and dispatch.

Scope and identities are supplied by the caller: Skills receive prepared JSON material
only and never fetch or filter records. Reviewed prompts resolve by user and function.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Generic, Literal, TypeVar
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, field_validator, model_validator

from llm import LLMError, PromptRepository, PromptService
from schema import StrictModel

logger = logging.getLogger(__name__)

# --- Structured contracts: scope, records, input, result ---

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


class ProgressItem(TextModel):
    work_item: str = Field(min_length=1)
    # S04 的输入只有姓名，没有人员编号，所以参与者按姓名引用（见 S04Skill.prompt_payload）。
    participant_names: list[str] = Field(min_length=1)
    current_progress: str = Field(min_length=1)
    main_difficulties: str = ""


class ProgressOutput(TextModel):
    items: list[ProgressItem]


class PersonnelItem(TextModel):
    name: str = Field(min_length=1)
    result: Literal["拟降级", "拟奖励", "拟退出", "拟提拔"]
    reason: str = Field(min_length=1)


class PersonnelOutput(TextModel):
    items: list[PersonnelItem]


class IdeaItem(TextModel):
    idea: str = Field(min_length=1)
    proposers: list[str] = Field(min_length=1)


class IdeaOutput(TextModel):
    items: list[IdeaItem]


class MeetingFocusItem(TextModel):
    name: str = Field(min_length=1)
    focus: list[Literal["思想或态度沟通", "持续困难", "兴趣", "好idea", "培养潜力"]] = Field(min_length=1)
    reason: str = Field(min_length=1)


class MeetingFocusOutput(TextModel):
    items: list[MeetingFocusItem]


class TechnologyItem(TextModel):
    name: str = Field(min_length=1)
    type: Literal["工程", "科研"] | None = None
    problem: str = Field(min_length=1)
    contributors: list[str] = Field(min_length=1)
    achievements: list[str] = Field(default_factory=list)
    usage: list[str] = Field(default_factory=list)
    suggestion: str = Field(min_length=1)
    todo: list[str] = Field(default_factory=list)


class TechnologyOutput(TextModel):
    items: list[TechnologyItem]


class TrainingItem(TextModel):
    topic: str = Field(min_length=1)
    audience: list[str] = Field(default_factory=list)
    needs: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    goals: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    method: str = Field(min_length=1)
    suggestion: str = Field(min_length=1)


class TrainingOutput(TextModel):
    items: list[TrainingItem]


OutputT = TypeVar("OutputT", bound=StrictModel)


class SkillResult(StrictModel, Generic[OutputT]):
    result_id: str
    run_id: str
    skill_code: SkillCode
    user_id: str
    prompt_source: Literal["TABLE", "BUILTIN"]
    prompt_record_id: str | None = None
    status: SkillStatus
    scope: MaterialScope
    generated_at: datetime
    content: OutputT | None = None
    message: str = ""
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None
    material_statistics: dict[str, int] | None = None


# --- Shared execution boundary ---

class TextSkill(Generic[OutputT]):
    code: SkillCode
    version = "1.0"
    output_model: type[OutputT]
    requires_confirmed = False
    array_root = False  # S05-S09 输出顶层 JSON 数组；S04 输出 {"items":[...]}

    def validate_input(self, data: SkillInput) -> None:
        """Specialized Skills may add structural constraints, never filtering."""

    def prompt_payload(self, data: SkillInput) -> StrictModel | str:
        """默认发材料 JSON；只汇总日志原文的 Skill 改发纯文本。"""
        return data

    def validate_output(self, output: OutputT, data: SkillInput) -> None:
        """结论锚定在本次材料里真实出现的人名上（姓名化，不再用编号）。"""
        names = {record.name for record in data.records if record.submitted}
        for item in getattr(output, "items", []):
            for field in ("participant_names", "proposers", "contributors"):
                values = getattr(item, field, None)
                if values is not None:
                    self._validate_names(field, values, names)

    @staticmethod
    def _validate_names(field: str, values: list[str], allowed: set[str]) -> None:
        if len(values) != len(set(values)) or not set(values) <= allowed:
            raise ValueError(f"{field} 重复或不在本次材料人员名单内")

    def run(self, service: PromptService, data: SkillInput, *, user_id: str,
            prompts: PromptRepository | None = None) -> SkillResult[OutputT]:
        # Revalidate mutable Pydantic instances at the call boundary.
        data = SkillInput.model_validate(data.model_dump())
        user_id = user_id.strip()
        if not user_id:
            raise ValueError("Skill 使用人编号不能为空")
        selected = prompts.resolve(self.code.value, user_id) if prompts else None

        def metadata() -> dict:
            source = "TABLE" if selected else "BUILTIN"
            record_id = selected.record_id if selected else None
            return dict(
                result_id=str(uuid5(NAMESPACE_URL,
                    json.dumps(["recordhub", data.run_id, self.code.value,
                                user_id, source, record_id], ensure_ascii=False))),
                run_id=data.run_id, skill_code=self.code, user_id=user_id,
                prompt_source=source, prompt_record_id=record_id,
                scope=data.scope.model_copy(deep=True),
                generated_at=datetime.now(timezone.utc),
            )
        if not data.scope.complete:
            return SkillResult[self.output_model](**metadata(),
                status=SkillStatus.BLOCKED, message="输入材料缺失，请对应负责人补齐材料")
        self.validate_input(data)
        submitted = [record for record in data.records if record.submitted]
        if not submitted:
            return SkillResult[self.output_model](**metadata(),
                status=SkillStatus.NO_MATERIAL, message="本范围内暂无可分析材料")
        if (self.requires_confirmed and not data.scope.omitted_sources
                and any(not record.confirmed and not record.evaluations for record in data.records)):
            return SkillResult[self.output_model](**metadata(),
                status=SkillStatus.BLOCKED, message="输入含未确认记录，请先完成确认")
        # Missing submissions never enter the prompt payload.
        payload = data.model_copy(update={"records": submitted}, deep=True)
        if self.code == SkillCode.S04:
            id_hint = "\n\nparticipant_names 只能取输入中出现过的姓名，原样引用，不得编造。"
        else:
            id_hint = ""
        payload = self.prompt_payload(payload)
        builtin_template = (Path('prompts') / f'{self.code.value}.txt').read_text(
            encoding='utf-8').strip()
        template = (selected.template if selected else builtin_template) + id_hint
        try:
            output = service.execute(
                prompt_code=self.code.value, template=template,
                input_data=payload, output_model=self.output_model,
                semantic_validator=lambda value: self.validate_output(value, data),
                array_root=self.array_root,
            )
        except LLMError:
            if selected is None:
                raise
            logger.warning(
                "personal_prompt_failed_fallback workflow=workflow2 code=%s "
                "user_id=%s record_id=%s", self.code.value, user_id,
                selected.record_id, exc_info=True)
            selected = None
            output = service.execute(
                prompt_code=self.code.value, template=builtin_template + id_hint,
                input_data=payload, output_model=self.output_model,
                semantic_validator=lambda value: self.validate_output(value, data),
                array_root=self.array_root,
            )
        return SkillResult[self.output_model](**metadata(),
            status=SkillStatus.WAITING_CONFIRMATION, content=output)


# --- Skill definitions ---

# S04: extract stage progress and difficulties from prepared check text.
def _s04_material(data: SkillInput) -> str:
    """S04 的输入是一段纯文本：日期 → 姓名 → 段落，靠缩进分三层，没填的段落不出现。

    只用姓名：阶段汇总读的是工作内容，编号对模型只是噪声。
    日志是整篇原文（没有分段）时给一行“完整日志:”，由模型自己分辨进展和困难。
    """
    blocks: list[str] = []
    for day in sorted({record.work_start for record in data.records}):
        lines = [f"{day:%m-%d}:"]
        for record in (item for item in data.records if item.work_start == day):
            lines.append(f"  {record.name}:")
            sections = (("进展", record.progress), ("困难", record.difficulties),
                        ("心得", record.reflection), ("其他", record.other))
            filled = [(label, text.strip()) for label, text in sections if text.strip()]
            lines.extend(f"    {label}: {text}" for label, text in filled)
            if not filled:
                lines.append(f"    完整日志: {record.full_log.strip()}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


class S04Skill(TextSkill[ProgressOutput]):
    code = SkillCode.S04
    output_model = ProgressOutput
    # 只汇总日志原文，材料里不含评价，所以不等评价定稿（见 MaterialPreparer.with_evaluations）。
    requires_confirmed = False

    def prompt_payload(self, data: SkillInput) -> str:
        return _s04_material(data)

    def validate_output(self, output: ProgressOutput, data: SkillInput) -> None:
        super().validate_output(output, data)
        names = {record.name for record in data.records if record.submitted}
        for item in output.items:
            if len(set(item.participant_names)) != len(item.participant_names):
                raise ValueError("participant_names 重复")
            if not set(item.participant_names) <= names:
                raise ValueError("participant_names 不在本次材料人员名单内")

# S05-S09 的输入是一段纯文本（姓名 / 日志原文 / 定稿版评价），不再发编号 JSON。

def _log_text(record: TextRecord) -> str:
    if record.full_log.strip():
        return record.full_log.strip()
    sections = (("工作进展", record.progress), ("工作困难", record.difficulties),
                ("心得反思", record.reflection), ("其他", record.other))
    filled = [f"{label}:{text.strip()}" for label, text in sections if text.strip()]
    return "\n".join(filled) or "未填写"


def _evaluation_text(record: TextRecord) -> str:
    reviews = record.evaluations
    if not reviews:
        return "无"
    review = reviews[0]
    parts = []
    if review.positive.strip():
        parts.append(f"肯定之处：{review.positive.strip()}")
    if review.improvement.strip():
        parts.append(f"改进之处：{review.improvement.strip()}")
    return "\n".join(parts) or "无"


def _periodic_material(data: SkillInput, *, with_backbone: bool) -> str:
    records = [record for record in data.records if record.submitted]
    blocks: list[str] = []
    if with_backbone:
        backbone = next((record for record in records if record.role == "骨干学生"), None)
        if backbone is not None:
            blocks.append(f"本次骨干：{backbone.name}")
    ordered = sorted(records, key=lambda record: (record.work_start, record.name))
    for record in ordered:
        blocks.append("\n".join([
            f"姓名：{record.name}",
            "日志原文：",
            _log_text(record),
            "定稿版评价：",
            _evaluation_text(record),
        ]))
    return "\n\n".join(blocks)


# S05: evidence-backed personnel suggestions, never personnel mutations.
class S05Skill(TextSkill[PersonnelOutput]):
    code = SkillCode.S05
    output_model = PersonnelOutput
    requires_confirmed = True
    array_root = True

    def prompt_payload(self, data: SkillInput) -> str:
        return _periodic_material(data, with_backbone=True)

    def validate_output(self, output: PersonnelOutput, data: SkillInput) -> None:
        super().validate_output(output, data)
        roles = {record.name: record.role for record in data.records if record.submitted}
        seen = set()
        for item in output.items:
            if item.name not in roles:
                raise ValueError("S05 的 name 不在本次材料人员名单内")
            if item.name in seen:
                raise ValueError("S05 同一人员重复")
            seen.add(item.name)
            required = "骨干学生" if item.result in {"拟降级", "拟奖励"} else "基层学生"
            if roles[item.name] != required:
                raise ValueError("S05 建议类型与该人员的角色不符")


# S06: identify ideas actually proposed in the supplied records.
class S06Skill(TextSkill[IdeaOutput]):
    code = SkillCode.S06
    output_model = IdeaOutput
    requires_confirmed = True
    array_root = True

    def prompt_payload(self, data: SkillInput) -> str:
        return _periodic_material(data, with_backbone=True)


# S07: prepare evidence-backed discussion topics for student meetings.
class S07Skill(TextSkill[MeetingFocusOutput]):
    code = SkillCode.S07
    output_model = MeetingFocusOutput
    requires_confirmed = True
    array_root = True

    def prompt_payload(self, data: SkillInput) -> str:
        return _periodic_material(data, with_backbone=True)

    def validate_output(self, output: MeetingFocusOutput, data: SkillInput) -> None:
        super().validate_output(output, data)
        names = {record.name for record in data.records if record.submitted}
        seen = set()
        for item in output.items:
            if item.name not in names:
                raise ValueError("S07 的 name 不在本次材料人员名单内")
            if item.name in seen or len(item.focus) != len(set(item.focus)):
                raise ValueError("S07 人员或关注类别重复")
            seen.add(item.name)


# S08: suggest reusable technical knowledge from supplied evidence.
class S08Skill(TextSkill[TechnologyOutput]):
    code = SkillCode.S08
    output_model = TechnologyOutput
    requires_confirmed = True
    array_root = True

    def prompt_payload(self, data: SkillInput) -> str:
        return _periodic_material(data, with_backbone=False)


# S09: suggest shared training using only supplied needs and resources.
class S09Skill(TextSkill[TrainingOutput]):
    code = SkillCode.S09
    output_model = TrainingOutput
    requires_confirmed = True
    array_root = True

    def prompt_payload(self, data: SkillInput) -> str:
        return _periodic_material(data, with_backbone=False)


# --- Registry and dispatch ---

SKILLS = {skill.code: skill for skill in (
    S04Skill(), S05Skill(),
    S06Skill(), S07Skill(), S08Skill(), S09Skill(),
)}


class SkillRunner:
    def __init__(self, service: PromptService,
                 prompts: PromptRepository | None = None) -> None:
        self.service = service
        self.prompts = prompts

    @staticmethod
    def output_schema(code: SkillCode | str) -> dict:
        return SKILLS[SkillCode(code)].output_model.model_json_schema()

    def run(self, code: SkillCode | str, data: SkillInput, *,
            user_id: str) -> SkillResult:
        code = SkillCode(code)
        return SKILLS[code].run(self.service, data, user_id=user_id.strip(),
                                prompts=self.prompts)

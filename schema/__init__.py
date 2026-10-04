from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowStatus(StrEnum):
    INITIALIZING = "INITIALIZING"
    SNAPSHOT_BUILDING = "SNAPSHOT_BUILDING"
    SNAPSHOT_READY = "SNAPSHOT_READY"
    LOGS_RUNNING = "LOGS_RUNNING"
    WAITING_EVALUATIONS = "WAITING_EVALUATIONS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class UnitStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"


class Person(StrictModel):
    person_id: str
    name: str
    role: str
    department_id: str | None = None
    leader_id: str | None = None
    open_id: str | None = None
    source_record_id: str | None = None
    active: bool = True


class Department(StrictModel):
    department_id: str
    name: str
    minister_id: str | None = None
    source_record_id: str | None = None
    active: bool = True


class Organization(StrictModel):
    persons: list[Person]
    departments: list[Department]
    team_leader_id: str | None = None
    anomalies: list[str] = Field(default_factory=list)

    def person_map(self) -> dict[str, Person]:
        return {person.person_id: person for person in self.persons}

    def department_map(self) -> dict[str, Department]:
        return {department.department_id: department for department in self.departments}

    def subordinates(self) -> dict[str, list[Person]]:
        result: dict[str, list[Person]] = {}
        for person in self.persons:
            if person.active and person.leader_id:
                result.setdefault(person.leader_id, []).append(person)
        for people in result.values():
            people.sort(key=lambda item: item.person_id)
        return result


class WorkLog(StrictModel):
    log_id: str
    source_record_id: str | None = None
    submitted_at: datetime
    person_id: str
    progress: str = ""
    difficulties: str = ""
    reflection: str = ""
    other: str = ""
    full_log: str = ""

    def content(self) -> str:
        if self.full_log.strip():
            return self.full_log.strip()
        return "\n\n".join(
            (
                f"工作进展：\n{self.progress or '未填写'}",
                f"工作困难：\n{self.difficulties or '未填写'}",
                f"心得反思：\n{self.reflection or '未填写'}",
                f"其他：\n{self.other or '未填写'}",
            )
        )


class SubmissionStatus(StrictModel):
    person_id: str
    submitted: bool
    log_id: str | None = None
    log_ids: list[str] = Field(default_factory=list)


class PromptConfig(StrictModel):
    config_id: str
    prompt_code: Literal["LOG"]
    version: str
    scope_type: Literal["PERSON", "DEPARTMENT", "TEAM"]
    scope_id: str
    template: str
    enabled: bool = True


class LogPromptInput(StrictModel):
    target_date: date
    person_id: str
    log_id: str
    log: str


class LogPromptOutput(StrictModel):
    positive: str
    improvement: str

    @model_validator(mode="after")
    def nonempty(self) -> "LogPromptOutput":
        if not self.positive.strip() or not self.improvement.strip():
            raise ValueError("逐日志评价的肯定和改进内容都不能为空")
        return self


class FinalEvaluation(StrictModel):
    person_id: str
    log_id: str
    evaluator_id: str
    positive: str
    improvement: str
    source: Literal["AI", "HUMAN"] = "AI"


class LogEvaluationState(StrictModel):
    log_id: str
    person_id: str
    evaluator_id: str
    status: UnitStatus = UnitStatus.PENDING
    positive_ai: str | None = None
    improvement_ai: str | None = None
    positive_final: str | None = None
    improvement_final: str | None = None
    source: Literal["AI", "HUMAN"] = "AI"
    manual_skipped: bool = False
    record_id: str | None = None
    evaluation_id: str | None = None
    evaluated_at: datetime | None = None
    error: str | None = None


class EvaluatorProgress(StrictModel):
    evaluator_id: str
    log_ids: list[str] = Field(default_factory=list)
    notified: bool = False
    closed: bool = False


class CloudObject(StrictModel):
    business_key: str
    token: str
    url: str
    folder_token: str | None = None
    content_written: bool = False


class DailySnapshot(StrictModel):
    schema_version: int = 2
    workflow_run_id: str
    target_date: date
    organization: Organization
    logs: list[WorkLog]
    submission_status: dict[str, SubmissionStatus]
    log_evaluations: dict[str, LogEvaluationState] = Field(default_factory=dict)
    evaluators: dict[str, EvaluatorProgress] = Field(default_factory=dict)
    cloud_objects: dict[str, CloudObject] = Field(default_factory=dict)
    evaluations_published: bool = False
    issues: dict[str, str] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class WorkflowRun(StrictModel):
    workflow_run_id: str
    workflow_type: str
    target_date: date
    status: WorkflowStatus
    current_stage: str
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    retry_count: int = 0
    last_error: str | None = None


class ConfirmationRequest(StrictModel):
    table_name: Literal["human_evaluations"]
    table_id: str
    record_id: str
    business_key: str
    modified_at: datetime | None = None


class PersonCreateRequest(StrictModel):
    person_id: str
    name: str
    role: str
    department_id: str | None = None
    leader_id: str | None = None
    open_id: str | None = None
    active: bool = True

    @model_validator(mode="after")
    def required_nonempty(self) -> "PersonCreateRequest":
        for field in ("person_id", "name", "role"):
            if not getattr(self, field).strip():
                raise ValueError(f"{field} 不能为空")
        return self

    def to_person(self) -> Person:
        return Person(
            person_id=self.person_id.strip(),
            name=self.name.strip(),
            role=self.role.strip(),
            department_id=(self.department_id or "").strip() or None,
            leader_id=(self.leader_id or "").strip() or None,
            open_id=(self.open_id or "").strip() or None,
            active=self.active,
        )


class PersonUpdateRequest(StrictModel):
    name: str | None = None
    role: str | None = None
    department_id: str | None = None
    leader_id: str | None = None
    open_id: str | None = None
    active: bool | None = None

    @model_validator(mode="after")
    def normalize(self) -> "PersonUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("至少需要提供一个要修改的字段")
        for field in ("name", "role"):
            if field in self.model_fields_set and not (getattr(self, field) or "").strip():
                raise ValueError(f"{field} 不能为空")
        for field in ("department_id", "leader_id", "open_id"):
            if field in self.model_fields_set and not (getattr(self, field) or "").strip():
                setattr(self, field, None)
        return self


class TableDefinition(StrictModel):
    table_id: str
    fields: dict[str, str]


class TableConfig(StrictModel):
    tables: dict[str, TableDefinition]


JsonDict = dict[str, Any]

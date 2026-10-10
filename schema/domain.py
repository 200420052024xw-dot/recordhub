"""Shared organization, log, evaluation, cloud and run models."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from schema.base import StrictModel, TableDefinition


class Person(StrictModel):
    person_id: str
    name: str
    role: str
    department_id: str | None = None
    leader_id: str | None = None
    open_id: str | None = None
    mobile: str | None = None
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

    def persons_named(self, name: str) -> list[Person]:
        """Active people whose name matches exactly.

        Names are unique by policy (see scripts/fill_organization_production.py),
        but nothing enforces it; callers must handle more than one hit rather
        than picking a person.
        """
        target = name.strip()
        if not target:
            return []
        return [person for person in self.persons
                if person.active and person.name.strip() == target]


class LogResource(StrictModel):
    resource_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    category: Literal["课程", "技术"]
    description: str = ""

    @field_validator("resource_id", "name", "description", mode="before")
    @classmethod
    def clean_text(cls, value):
        return value.strip() if isinstance(value, str) else value


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
    achievement_refs: list[str] = Field(default_factory=list)
    resources: list[LogResource] = Field(default_factory=list)

    @staticmethod
    def compose(progress: str, difficulties: str, reflection: str,
                other: str, full_log: str = "") -> str:
        """Single source of the 「完整日志」 text, shared by reads and writes."""
        if not any((progress, difficulties, reflection, other)):
            return full_log.strip()
        return "\n".join(
            f"{label}:{value.strip() or '未填写'}"
            for label, value in (
                ("工作进展", progress),
                ("工作困难", difficulties),
                ("心得反思", reflection),
                ("其他", other),
            )
        )

    def content(self) -> str:
        return self.compose(self.progress, self.difficulties,
                            self.reflection, self.other, self.full_log)


class SubmissionStatus(StrictModel):
    person_id: str
    submitted: bool
    log_id: str | None = None
    log_ids: list[str] = Field(default_factory=list)


class FinalEvaluation(StrictModel):
    person_id: str
    log_id: str
    evaluator_id: str
    positive: str
    improvement: str
    source: Literal["AI", "HUMAN"] = "AI"


class CloudObject(StrictModel):
    business_key: str
    token: str
    url: str
    folder_token: str | None = None
    content_written: bool = False


class WorkflowRun(StrictModel):
    workflow_run_id: str
    workflow_type: str
    target_date: date
    status: str
    current_stage: str
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    retry_count: int = 0
    last_error: str | None = None


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


class TableConfig(StrictModel):
    tables: dict[str, TableDefinition]

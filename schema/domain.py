"""Shared organization, log, evaluation, cloud and run models."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from schema.base import StrictModel, TableDefinition


class Membership(StrictModel):
    """A person's role in one department.

    Primary membership is mirrored on ``Person.department_id/leader_id/role``
    for backward compatibility; extra departments live only here.
    """

    department_id: str
    role: str
    leader_id: str | None = None
    primary: bool = False


class Person(StrictModel):
    person_id: str
    name: str
    role: str
    department_id: str | None = None
    leader_id: str | None = None
    memberships: list[Membership] = Field(default_factory=list)
    open_id: str | None = None
    mobile: str | None = None
    source_record_id: str | None = None
    active: bool = True

    def membership_list(self) -> list[Membership]:
        """All memberships, synthesizing the primary one for legacy data."""
        if self.memberships:
            return self.memberships
        if not self.department_id:
            return []
        return [Membership(department_id=self.department_id, role=self.role,
                           leader_id=self.leader_id, primary=True)]


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

    def memberships_of(self, person_id: str) -> list[Membership]:
        person = self.person_map().get(person_id)
        return person.membership_list() if person else []

    def membership(self, person_id: str, department_id: str) -> Membership | None:
        for item in self.memberships_of(person_id):
            if item.department_id == department_id:
                return item
        return None

    def person_in_department(self, person_id: str,
                             department_id: str) -> Person | None:
        person = self.person_map().get(person_id)
        return person if person and self.membership(person_id, department_id) else None

    def departments_of(self, person_id: str) -> list[str]:
        return [item.department_id for item in self.memberships_of(person_id)]

    def evaluator_for(self, person_id: str, department_id: str) -> str | None:
        item = self.membership(person_id, department_id)
        return item.leader_id if item else None

    def members_in_department(self, department_id: str,
                              roles: set[str] | None = None) -> list[tuple[Person, Membership]]:
        result: list[tuple[Person, Membership]] = []
        for person in self.persons:
            if not person.active:
                continue
            for item in person.membership_list():
                if item.department_id != department_id:
                    continue
                if roles is not None and item.role not in roles:
                    continue
                result.append((person, item))
        result.sort(key=lambda pair: pair[0].person_id)
        return result

    def reports_of(self, evaluator_id: str,
                   department_id: str | None = None) -> list[tuple[Person, Membership]]:
        result: list[tuple[Person, Membership]] = []
        for person in self.persons:
            if not person.active:
                continue
            for item in person.membership_list():
                if item.leader_id != evaluator_id:
                    continue
                if department_id is not None and item.department_id != department_id:
                    continue
                result.append((person, item))
        result.sort(key=lambda pair: pair[0].person_id)
        return result

    def subordinates(self) -> dict[str, list[Person]]:
        """Direct reports across all memberships, deduplicated by person."""
        grouped: dict[str, list[Person]] = {}
        for person in self.persons:
            if not person.active:
                continue
            for item in person.membership_list():
                if item.leader_id:
                    grouped.setdefault(item.leader_id, []).append(person)
        result: dict[str, list[Person]] = {}
        for leader_id, people in grouped.items():
            seen: set[str] = set()
            unique = []
            for person in people:
                if person.person_id not in seen:
                    seen.add(person.person_id)
                    unique.append(person)
            unique.sort(key=lambda item: item.person_id)
            result[leader_id] = unique
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
    final_evaluation: str = ""
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
        department_id = (self.department_id or "").strip() or None
        leader_id = (self.leader_id or "").strip() or None
        role = self.role.strip()
        return Person(
            person_id=self.person_id.strip(),
            name=self.name.strip(),
            role=role,
            department_id=department_id,
            leader_id=leader_id,
            memberships=([Membership(department_id=department_id, role=role,
                                     leader_id=leader_id, primary=True)]
                         if department_id else []),
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

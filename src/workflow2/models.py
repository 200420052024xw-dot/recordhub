"""Persisted Workflow2 state and the team synthesis contract."""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field

from schema.base import StrictModel
from workflow2.skills import SkillInput


class MaterialStatistics(StrictModel):
    expected_people: int = 0
    submitted_people: int = 0
    missing_people: int = 0
    expected_person_days: int = 0
    submitted_person_days: int = 0
    missing_person_days: int = 0


class PreparedMaterial(StrictModel):
    input: SkillInput
    statistics: MaterialStatistics | None


class DepartmentResult(StrictModel):
    department_id: str
    minister_id: str
    drafts: dict[str, dict[str, Any]] = Field(default_factory=dict)
    draft_text: dict[str, str] = Field(default_factory=dict)
    final_text: dict[str, str] = Field(default_factory=dict)
    unconfirmed: list[str] = Field(default_factory=list)
    confirmation_record_id: str | None = None


class CycleRun(StrictModel):
    run_id: str
    kind: Literal["stage", "monthly", "weekly"]
    scheduled_date: date
    start_date: date
    end_date: date
    status: Literal["PENDING", "WAITING_CONFIRMATION", "BLOCKED", "FAILED", "COMPLETED"] = "PENDING"
    departments: dict[str, DepartmentResult] = Field(default_factory=dict)
    team_results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    weekly_reference: dict[str, dict[str, Any]] = Field(default_factory=dict)
    department_results: dict[str, dict[str, dict[str, Any]]] = Field(default_factory=dict)
    document_tokens: dict[str, str] = Field(default_factory=dict)
    document_urls: dict[str, str] = Field(default_factory=dict)
    notification_ids: dict[str, str] = Field(default_factory=dict)
    issues: dict[str, str] = Field(default_factory=dict)
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class DepartmentAnalysis(StrictModel):
    """One department's S08/S09 draft items, fed to the weekly summary call."""

    department_id: str
    department_name: str
    items: list[dict[str, Any]] = Field(default_factory=list)


class WeeklySummaryInput(StrictModel):
    skill_code: str
    start_date: date
    end_date: date
    reference_items: list[dict[str, Any]] = Field(default_factory=list)
    departments: list[DepartmentAnalysis] = Field(default_factory=list)

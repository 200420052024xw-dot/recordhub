"""Persistent periodic-analysis contracts, separate from the daily state schema."""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field

from schema import StrictModel
from skills import SkillCode, SkillConfig, SkillInput


class AnalysisRequest(StrictModel):
    skill_code: SkillCode
    start_date: date
    end_date: date
    user_id: str = Field(min_length=1)
    department_ids: list[str] | None = None
    revision: str = Field(default="1", min_length=1)


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


class AnalysisRun(StrictModel):
    schema_version: Literal[1] = 1
    run_id: str
    request: AnalysisRequest
    config_department_id: str
    scope_type: Literal["DEPARTMENT", "TEAM"]
    status: Literal["PENDING", "RUNNING", "BLOCKED", "FAILED", "WAITING_CONFIRMATION", "CONFIRMED", "NO_MATERIAL"] = "PENDING"
    config: SkillConfig | None = None
    material: PreparedMaterial | None = None
    draft_result: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    external_record_id: str | None = None
    notification_reserved_at: datetime | None = None
    notification_message_id: str | None = None
    blocked_notification_reserved_at: datetime | None = None
    blocked_notification_message_id: str | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class AnalysisConfirmation(StrictModel):
    user_id: str = Field(min_length=1)
    content: dict[str, Any] | None = None


class NotificationReconciliation(StrictModel):
    message_id: str = Field(min_length=1)
    blocked: bool = False

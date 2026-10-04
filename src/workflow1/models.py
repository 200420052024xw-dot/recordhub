"""Workflow1 stages, prompt contracts, evaluation progress and snapshot."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from schema.base import StrictModel
from schema.domain import CloudObject, Organization, SubmissionStatus, WorkLog


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
    ai_evaluated_at: datetime | None = None
    confirmed_at: datetime | None = None
    error: str | None = None


class EvaluatorProgress(StrictModel):
    evaluator_id: str
    log_ids: list[str] = Field(default_factory=list)
    notified: bool = False
    closed: bool = False


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


class ConfirmationRequest(StrictModel):
    table_name: Literal["human_evaluations"]
    table_id: str
    record_id: str
    business_key: str
    modified_at: datetime | None = None

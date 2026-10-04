"""Load and validate editable workflow schedules."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_SCHEDULE_TYPES = {"daily", "weekly", "monthly", "interval_days"}
_WEEKDAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


@dataclass(frozen=True, slots=True)
class WorkflowSchedule:
    name: str
    enabled: bool
    schedule_type: str
    time: str
    depends_on: tuple[str, ...] = ()
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    timezone: str
    workflows: dict[str, WorkflowSchedule]


def load_schedule_config(
    path: str | Path = "config/schedules.toml",
) -> ScheduleConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ValueError(f"Schedule configuration does not exist: {config_path}")
    with config_path.open("rb") as stream:
        raw = tomllib.load(stream)
    timezone = str(raw.get("timezone", ""))
    # Windows may not ship an IANA timezone database. RecordHub's required
    # timezone is therefore accepted explicitly; other zones still use the
    # platform database when available.
    if timezone != "Asia/Shanghai":
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown schedule timezone: {timezone}") from exc

    raw_workflows = raw.get("workflows")
    if not isinstance(raw_workflows, dict) or not raw_workflows:
        raise ValueError("Schedule configuration must contain [workflows.*] entries")
    workflows: dict[str, WorkflowSchedule] = {}
    for name, values in raw_workflows.items():
        if not isinstance(values, dict):
            raise ValueError(f"Workflow schedule {name} must be a table")
        schedule_type = str(values.get("schedule_type", ""))
        run_time = str(values.get("time", ""))
        if schedule_type not in _SCHEDULE_TYPES:
            raise ValueError(f"Workflow schedule {name} has invalid schedule_type")
        if not _TIME_PATTERN.fullmatch(run_time):
            raise ValueError(f"Workflow schedule {name} has invalid time: {run_time}")
        _validate_type_options(name, schedule_type, values)
        depends_on = tuple(str(item) for item in values.get("depends_on", []))
        options = {
            key: value
            for key, value in values.items()
            if key not in {"enabled", "schedule_type", "time", "depends_on"}
        }
        workflows[name] = WorkflowSchedule(
            name=name,
            enabled=bool(values.get("enabled", True)),
            schedule_type=schedule_type,
            time=run_time,
            depends_on=depends_on,
            options=options,
        )
    for workflow in workflows.values():
        unknown = [
            dependency
            for dependency in workflow.depends_on
            if dependency not in workflows
        ]
        if unknown:
            raise ValueError(
                f"Workflow schedule {workflow.name} has unknown dependencies: "
                f"{', '.join(unknown)}"
            )
    _check_dependency_cycles(workflows)
    return ScheduleConfig(timezone=timezone, workflows=workflows)


def _validate_type_options(name: str, schedule_type: str, values: dict[str, Any]) -> None:
    if schedule_type == "weekly" and values.get("weekday") not in _WEEKDAYS:
        raise ValueError(f"Weekly task {name} needs weekday mon..sun")
    if schedule_type == "monthly":
        day = values.get("day")
        if not isinstance(day, int) or not 1 <= day <= 28:
            raise ValueError(f"Monthly task {name} needs day between 1 and 28")
    if schedule_type == "interval_days":
        every_days = values.get("every_days")
        if not isinstance(every_days, int) or every_days < 1:
            raise ValueError(f"Interval task {name} needs positive every_days")
        try:
            date.fromisoformat(str(values.get("anchor_date", "")))
        except ValueError as exc:
            raise ValueError(f"Interval task {name} needs ISO anchor_date") from exc


def _check_dependency_cycles(workflows: dict[str, WorkflowSchedule]) -> None:
    visited: set[str] = set()
    active: set[str] = set()

    def visit(name: str) -> None:
        if name in active:
            raise ValueError(f"Schedule dependency cycle detected at {name}")
        if name in visited:
            return
        active.add(name)
        for dependency in workflows[name].depends_on:
            visit(dependency)
        active.remove(name)
        visited.add(name)

    for workflow_name in workflows:
        visit(workflow_name)



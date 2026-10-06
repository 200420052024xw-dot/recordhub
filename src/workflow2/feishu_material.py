"""Rebuild daily check snapshots from the Feishu source tables.

Workflow1 daily snapshots are processing caches that are really deleted after
retention, so weekly and monthly windows reconstruct the missing days straight
from the log table, the AI evaluation table and the human evaluation table.
Confirmation semantics mirror Workflow1: a complete human row finalizes per its
two flags (确认无误 keeps the AI text, 需修改 keeps the edited text), and a day
past the auto-advance deadline without a human row keeps the AI texts as final.
Each table is read once per rebuild; days with no AI row stay unconfirmed and
are reported as missing material by MaterialPreparer.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from data.repositories import LogRepository, OrganizationCache
from data.store import utc_now
from schema import (Organization, SubmissionStatus, TableConfig, WorkLog)
from tool.bitable_fields import (SHANGHAI, alias_index, field_datetime,
                                  record_fields, record_submission_time,
                                  resolve_reference, scalar)
from tool.feishu import BitableService
from workflow1.models import (DailySnapshot, EvaluatorProgress,
                              LogEvaluationState, UnitStatus)

_AI_SENTINEL = "\0ai\0"


class SnapshotRebuilder:
    def __init__(self, bitable: BitableService, tables: TableConfig,
                 organization_cache: OrganizationCache,
                 log_repository: LogRepository, *,
                 auto_advance_at: str = "12:00") -> None:
        self.bitable = bitable
        self.tables = tables
        self.organization_cache = organization_cache
        self.log_repository = log_repository
        self.auto_advance_at = auto_advance_at

    def rebuild(self, start: date, end: date) -> dict[date, DailySnapshot]:
        organization = self.organization_cache.get()
        logs_by_day = self.log_repository.logs_in_window(start, end, organization)
        ai_rows = self._read_ai_rows(organization)
        human_rows = self._read_human_rows(organization, ai_rows)
        people = organization.person_map()
        now = utc_now()
        rebuilt: dict[date, DailySnapshot] = {}
        day = start
        while day <= end:
            rebuilt[day] = self._assemble(
                day, logs_by_day.get(day, ([], {}))[0], ai_rows, human_rows,
                organization, people, now)
            day += timedelta(days=1)
        return rebuilt

    def _assemble(self, day: date, logs: list[WorkLog],
                  ai_rows: dict, human_rows: dict, organization: Organization,
                  people: dict, now: datetime) -> DailySnapshot:
        submission_status: dict[str, SubmissionStatus] = {}
        log_evaluations: dict[str, LogEvaluationState] = {}
        evaluators: dict[str, EvaluatorProgress] = {}
        logs_by_person: dict[str, WorkLog] = {log.person_id: log for log in logs}
        eligible = sorted(
            (person for person in people.values()
             if person.active and person.role in {"基层学生", "骨干学生"}),
            key=lambda person: person.person_id)
        auto_confirmed = self._deadline_passed(day, now)
        for person in eligible:
            log = logs_by_person.get(person.person_id)
            if log is None:
                submission_status[person.person_id] = SubmissionStatus(
                    person_id=person.person_id, submitted=False)
                continue
            submission_status[person.person_id] = SubmissionStatus(
                person_id=person.person_id, submitted=True,
                log_id=log.log_id, log_ids=[log.log_id])
            state = LogEvaluationState(
                log_id=log.log_id, person_id=person.person_id,
                evaluator_id=person.leader_id or "", status=UnitStatus.WAITING_CONFIRMATION)
            ai_row = ai_rows.get((day, log.log_id))
            if ai_row is not None and ai_row["person_id"] == person.person_id:
                state.evaluator_id = ai_row["evaluator_id"]
                state.record_id = ai_row["record_id"]
                state.evaluation_id = ai_row["key"]
                state.positive_ai = ai_row["positive"]
                state.improvement_ai = ai_row["improvement"]
                state.ai_evaluated_at = ai_row["evaluated_at"]
                state.evaluated_at = ai_row["evaluated_at"]
                human_row = human_rows.get((day, log.log_id))
                finalized = self._finalize(human_row, ai_row)
                if finalized is not None:
                    positive, improvement, evaluated_at = finalized
                    state.positive_final = positive
                    state.improvement_final = improvement
                    state.source = "HUMAN"
                    state.confirmed_at = evaluated_at or state.evaluated_at
                    state.status = UnitStatus.CONFIRMED
                elif auto_confirmed and ai_row["positive"] and ai_row["improvement"]:
                    state.positive_final = ai_row["positive"]
                    state.improvement_final = ai_row["improvement"]
                    state.source = "AI"
                    state.manual_skipped = True
                    state.status = UnitStatus.CONFIRMED
            log_evaluations[log.log_id] = state
            if state.evaluator_id:
                progress = evaluators.setdefault(
                    state.evaluator_id,
                    EvaluatorProgress(evaluator_id=state.evaluator_id))
                progress.log_ids.append(log.log_id)
                progress.closed = state.status == UnitStatus.CONFIRMED
        return DailySnapshot(
            schema_version=2, workflow_run_id=f"feishu-rebuild:{day.isoformat()}",
            target_date=day, organization=organization, logs=logs,
            submission_status=submission_status, log_evaluations=log_evaluations,
            evaluators=evaluators, evaluations_published=True, issues={},
            created_at=now, updated_at=now)

    @staticmethod
    def _finalize(human_row: dict | None, ai_row: dict) -> tuple[str, str, Any] | None:
        """Resolve the human row against the AI texts, mirroring W1 confirmation."""
        if human_row is None or human_row["evaluator_id"] != ai_row["evaluator_id"]:
            return None
        positive = ai_row["positive"] if human_row["positive"] == _AI_SENTINEL else human_row["positive"]
        improvement = (ai_row["improvement"] if human_row["improvement"] == _AI_SENTINEL
                       else human_row["improvement"])
        if not positive or not improvement:
            return None
        return positive, improvement, human_row["evaluated_at"]

    def _deadline_passed(self, day: date, now: datetime) -> bool:
        if not self.auto_advance_at:
            return False
        hour, minute = map(int, self.auto_advance_at.split(":"))
        deadline = datetime.combine(day + timedelta(days=1), time(hour, minute), SHANGHAI)
        return now >= deadline

    def _person_index(self, organization: Organization):
        return alias_index(
            (person.person_id,
             (person.person_id, person.name, person.open_id or "",
              person.source_record_id or ""))
            for person in organization.persons)

    def _read_ai_rows(self, organization: Organization) -> dict[tuple[date, str], dict]:
        table = self.tables.tables.get("evaluations")
        rows: dict[tuple[date, str], dict] = {}
        if table is None or not table.table_id.strip():
            return rows
        f = table.fields
        key_field = f.get("business_key") or f["evaluation_id"]
        people = self._person_index(organization)
        basic_names = alias_index(
            (person.person_id, (person.name,)) for person in organization.persons
            if person.role == "基层学生" and person.active)
        for record in self.bitable.list_records(table.table_id):
            values = record_fields(record)
            parts = self._eval_key(scalar(values.get(key_field)))
            if parts is None:
                continue  # 未填写 marker rows and malformed keys are never material
            day, evaluator_id, log_id = parts
            name = scalar(values.get(f.get("person_name", ""))).strip()
            legacy_ref = resolve_reference(values.get(f["person_ref"]), people)
            if name and legacy_ref and legacy_ref != basic_names.get(name):
                continue
            person_id = (basic_names.get(name, "") if name else
                         legacy_ref)
            if not person_id:
                continue
            rows.setdefault((day, log_id), {
                "key": scalar(values.get(key_field)),
                "evaluator_id": evaluator_id, "person_id": person_id,
                "positive": scalar(values.get(f["positive_ai"])),
                "improvement": scalar(values.get(f["improvement_ai"])),
                "source_log": scalar(values.get(f["source_log"])),
                "evaluated_at": self._timestamp(values.get(f.get("evaluated_at"))),
                "record_id": str(record.get("record_id", "")) or None,
            })
        return rows

    def _read_human_rows(self, organization: Organization,
                         ai_rows: dict[tuple[date, str], dict]) -> dict[tuple[date, str], dict]:
        table = self.tables.tables.get("human_evaluations")
        rows: dict[tuple[date, str], dict] = {}
        if table is None or not table.table_id.strip():
            return rows
        f = table.fields
        people = self._person_index(organization)
        basic_names = alias_index(
            (person.person_id, (person.name,)) for person in organization.persons
            if person.role == "基层学生" and person.active)
        for record in self.bitable.list_records(table.table_id):
            values = record_fields(record)
            submitted_at = record_submission_time(record, f.get("evaluated_at", ""))
            if submitted_at is None:
                continue
            parts = self._eval_key(scalar(values.get(f["evaluation_id"])))
            if parts is None:
                name = scalar(values.get(f.get("basic_name", ""))).strip()
                if name and resolve_reference(values.get(f.get("backbone_ref", "")), people):
                    continue
                person_id = (basic_names.get(name, "") if name else
                             resolve_reference(values.get(
                                 f.get("backbone_ref") or f.get("person_ref", "")), people))
                evaluator_id = resolve_reference(values.get(f["submitted_by"]), people)
                original = scalar(values.get(f.get("source_log", ""))).strip()
                candidates = [(day, log_id) for (day, log_id), ai in ai_rows.items()
                              if ai["person_id"] == person_id
                              and ai["evaluator_id"] == evaluator_id
                              and (not original or ai["source_log"] == original)
                               and self._human_within_window(day, submitted_at)]
                if len(candidates) != 1:
                    continue
                day, log_id = candidates[0]
            else:
                day, evaluator_id, log_id = parts
            if not self._human_within_window(day, submitted_at):
                continue
            positive = self._final_text(values, f, "positive_confirmed", "positive_final")
            improvement = self._final_text(values, f, "improvement_confirmed", "improvement_final")
            if positive is None or improvement is None:
                continue  # half-filled or invalid rows never finalize
            rows.setdefault((day, log_id), {
                "evaluator_id": evaluator_id, "positive": positive,
                "improvement": improvement,
                "evaluated_at": self._timestamp(values.get(f.get("evaluated_at"))),
            })
        return rows

    def _human_within_window(self, day: date, submitted_at: datetime) -> bool:
        start = datetime.combine(day + timedelta(days=1), time.min, SHANGHAI)
        if self.auto_advance_at:
            hour, minute = map(int, self.auto_advance_at.split(":"))
            end = datetime.combine(day + timedelta(days=1), time(hour, minute), SHANGHAI)
        else:
            end = datetime.combine(day + timedelta(days=2), time.min, SHANGHAI)
        return start <= submitted_at < end

    @staticmethod
    def _final_text(values: dict, f: dict, flag_field: str, text_field: str) -> str | None:
        """Return the effective text, _AI_SENTINEL when the AI text is kept."""
        if f.get(flag_field) and f.get(text_field):
            flag = scalar(values.get(f[flag_field])).strip()
            if flag == "确认无误":
                return _AI_SENTINEL
            if flag == "需修改":
                return scalar(values.get(f[text_field])).strip() or None
            return None  # empty or invalid selection: the row never finalized
        return scalar(values.get(f.get(text_field, ""))).strip() or None

    @staticmethod
    def _eval_key(raw: str) -> tuple[date, str, str] | None:
        parts = raw.split(":")
        if len(parts) != 4 or parts[1] != "EVAL":
            return None
        try:
            return date.fromisoformat(parts[0]), parts[2], parts[3]
        except ValueError:
            return None

    @staticmethod
    def _timestamp(raw) -> datetime | None:
        if not raw:
            return None
        try:
            return field_datetime(raw)
        except (ValueError, TypeError):
            return None

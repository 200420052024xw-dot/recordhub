"""Run Workflow1 against today's live logs without publishing preview output.

The normal log reader and LLM are used. Evaluation-table writes, messages,
report rows and cloud documents are captured by local preview adapters.
The run state lives in a temporary directory and is removed on exit.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from config import AppSettings, load_env_file
from data import FileStateStore
from service.runtime import build_runtime
from workflow1.models import DailySnapshot
from workflow1.workflow import Workflow1


class PreviewEvaluations:
    def publish(self, snapshot: DailySnapshot) -> tuple[dict, dict]:
        return {}, {}


class PreviewLogs:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def get_logs_by_date(self, *args: Any, **kwargs: Any) -> Any:
        return self.repository.get_logs_by_date(*args, **kwargs)

    def backfill_full_logs(self, logs: Any) -> int:
        return 0


class PreviewHumanEvaluations:
    def for_date(self, snapshot: DailySnapshot) -> list:
        return []


class PreviewMessages:
    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def send_text(
        self, open_id: str, message: str, *, idempotency_key: str | None = None
    ) -> dict[str, str]:
        self.sent.append({"recipient": open_id, "text": message})
        return {"message_id": f"preview-{len(self.sent)}"}


class ConfirmationMessages:
    """Send real messages to the configured test account with fresh UUIDs."""

    def __init__(self, messages: Any) -> None:
        self.messages = messages
        self.batch_id = str(uuid4())
        self.message_ids: list[str] = []

    def send_text(
        self, open_id: str, message: str, *, idempotency_key: str | None = None
    ) -> dict:
        result = self.messages.send_text(
            open_id, message,
            idempotency_key=f"preview:{self.batch_id}:{idempotency_key or uuid4()}",
        )
        self.message_ids.append(str(result.get("message_id", "")))
        return result


class PreviewDocuments:
    def advance(self, target_date: date, *, on_issue: Any) -> dict:
        return {}


class PreviewReports:
    def publish(self, snapshot: DailySnapshot, objects: dict) -> dict:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview Workflow1 with live logs")
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--send-confirmations", action="store_true",
                        help="send confirmation messages for the saved snapshot to the test account")
    args = parser.parse_args()
    target_date = args.date or datetime.now(ZoneInfo("Asia/Shanghai")).date()

    load_env_file(ROOT / ".env")
    settings = AppSettings.from_env()
    runtime = build_runtime(settings)
    binding = runtime.workflows["workflow1_daily"]
    live = binding.workflow
    if args.send_confirmations:
        if not settings.message_override_open_id:
            parser.error("RECORDHUB_MESSAGE_OVERRIDE_OPEN_ID must be set")
        snapshot = binding.store.load_snapshot(target_date)
        if snapshot is None:
            parser.error(f"No saved Workflow1 snapshot for {target_date}")
        sender = ConfirmationMessages(runtime.infrastructure.messages)
        with tempfile.TemporaryDirectory(prefix="recordhub-w1-confirmation-") as directory:
            store = FileStateStore(
                directory, workflow_type="workflow1", snapshot_model=DailySnapshot
            )
            store.get_or_create_workflow(target_date)
            store.save_snapshot(snapshot)
        workflow = Workflow1(
            store=store,
            organization_cache=runtime.organization_cache,
            log_repository=PreviewLogs(live.log_repository),
                ai_evaluations=live.ai_evaluations,
                human_evaluations=live.human_evaluations,
                prompt_service=live.prompt_service,
                messages=sender,
                documents=live.documents,
                reports=live.reports,
                auto_advance_at="",
            )
            workflow._notify_evaluators(target_date)
        print(json.dumps({"date": str(target_date),
                          "sent_to_test_account": len(sender.message_ids),
                          "message_ids": sender.message_ids}, indent=2))
        return 0
    messages = PreviewMessages()
    print(f"Workflow1 preview started for {target_date}", flush=True)

    with tempfile.TemporaryDirectory(prefix="recordhub-w1-preview-") as directory:
        store = FileStateStore(
            directory, workflow_type="workflow1", snapshot_model=DailySnapshot
        )
        workflow = Workflow1(
            store=store,
            organization_cache=runtime.organization_cache,
            log_repository=PreviewLogs(live.log_repository),
            ai_evaluations=PreviewEvaluations(),
            human_evaluations=PreviewHumanEvaluations(),
            prompt_service=live.prompt_service,
            messages=messages,
            documents=PreviewDocuments(),
            reports=PreviewReports(),
            llm_concurrency=settings.llm_concurrency,
            auto_advance_at="",
            prompt_path=str(ROOT / "prompts" / "S01.txt"),
        )
        run = workflow.start(target_date)
        snapshot = store.load_snapshot(target_date)
        if snapshot is None:
            print(json.dumps({"date": str(target_date), "status": run.status,
                              "error": run.last_error}, ensure_ascii=False, indent=2))
            return 1

        people = snapshot.organization.person_map()
        logs = []
        for log in snapshot.logs:
            evaluation = snapshot.log_evaluations[log.log_id]
            logs.append({
                "log_id": log.log_id,
                "person": people[log.person_id].name,
                "submitted_at": log.submitted_at.isoformat(),
                "log": log.content(),
                "status": evaluation.status,
                "positive": evaluation.positive_ai,
                "improvement": evaluation.improvement_ai,
                "error": evaluation.error,
            })
        report = {
            "date": str(target_date),
            "status": run.status,
            "log_count": len(logs),
            "issues": snapshot.issues,
            "notifications_suppressed": len(messages.sent),
            "logs": logs,
        }
        report_path = ROOT / "data" / "state" / "previews" / f"workflow1-{target_date}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({
            "status": run.status,
            "log_count": len(logs),
            "issues_count": len(snapshot.issues),
            "notifications_suppressed": len(messages.sent),
            "report": str(report_path),
        }, ensure_ascii=True, indent=2))
        return 0 if run.status != "FAILED" else 1


if __name__ == "__main__":
    raise SystemExit(main())

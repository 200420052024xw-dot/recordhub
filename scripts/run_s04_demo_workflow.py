"""Run the complete S04 Workflow2 cycle with synthetic local daily materials.

The daily inputs and Workflow2 state live under data/state/s04_demo. Starting
the cycle still calls DeepSeek and writes to the configured Feishu S04 tables,
sends a clearly marked test notification to the .env override recipient, and
uses the configured confirmation form. The source log table is untouched.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
DEMO_ROOT = ROOT / "data" / "state" / "s04_demo"
SOURCE_DAY = date(2026, 10, 4)
SCHEDULED_DAY = date(2026, 10, 5)
SHANGHAI = ZoneInfo("Asia/Shanghai")

from config import AppSettings, load_env_file
from data import FileStateStore, utc_now
from schema import Organization, WorkLog
from service.runtime import build_runtime
from workflow1.models import DailySnapshot, UnitStatus
from workflow1.snapshot import SnapshotBuilder
from workflow2.materials import MaterialPreparer


class DemoMessages:
    def __init__(self, live_messages):
        self.live_messages = live_messages
        self.batch_id = str(uuid4())

    def send_text(self, open_id, message, *, idempotency_key=None):
        return self.live_messages.send_text(
            open_id, "【S04模拟测试】\n" + message,
            idempotency_key=f"demo:{self.batch_id}:{idempotency_key or uuid4()}")


def _organization() -> Organization:
    path = ROOT / "data" / "state" / "workflow1" / f"{SOURCE_DAY}.json"
    if not path.exists():
        raise ValueError(f"Completed Workflow1 source snapshot is missing: {path}")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state["run"]["status"] != "COMPLETED":
        raise ValueError("The 2026-10-04 Workflow1 source must be COMPLETED")
    return Organization.model_validate(state["snapshot"]["organization"])


def _participants(organization: Organization):
    people = organization.person_map()
    for department in organization.departments:
        if not department.active or not department.minister_id:
            continue
        minister = people[department.minister_id]
        if not minister.open_id:
            continue
        for backbone in organization.persons:
            if not (backbone.active and backbone.role == "骨干学生"
                    and backbone.department_id == department.department_id):
                continue
            member = next((person for person in organization.persons
                           if person.active and person.role == "基层学生"
                           and person.leader_id == backbone.person_id), None)
            if member is not None:
                return department.department_id, backbone.person_id, member.person_id
    raise ValueError("No active department with a minister, backbone, and member")


def prepare() -> tuple[str, int]:
    organization = _organization()
    department_id, backbone_id, member_id = _participants(organization)
    fixture = json.loads((ROOT / "examples" / "s04_input.json").read_text(encoding="utf-8"))
    identity = {"P001": backbone_id, "P002": member_id}
    store = FileStateStore(DEMO_ROOT, workflow_type="workflow1",
                           snapshot_model=DailySnapshot)
    store.save_cache("organization", organization.model_dump(mode="json"))
    grouped: dict[date, list[dict]] = {}
    for row in fixture["records"]:
        grouped.setdefault(date.fromisoformat(row["work_start"]), []).append(row)
    for day in (date(2026, 10, 2), date(2026, 10, 3), SOURCE_DAY):
        if store.load_workflow(day) is not None:
            continue
        run = store.get_or_create_workflow(day)
        logs = [WorkLog(
            log_id=row["record_id"], source_record_id=row["record_id"],
            person_id=identity[row["person_id"]],
            submitted_at=datetime.combine(day, time(20), SHANGHAI),
            progress="【模拟测试】" + row["progress"],
            difficulties=row["difficulties"], reflection=row["reflection"],
        ) for row in grouped[day]]
        snapshot = SnapshotBuilder().build(
            workflow_run_id=run.workflow_run_id, target_date=day,
            organization=organization, logs=logs, now=utc_now())
        for row in grouped[day]:
            state = snapshot.log_evaluations[row["record_id"]]
            evaluation = row["evaluations"][0]
            state.status = UnitStatus.CONFIRMED
            state.positive_ai = state.positive_final = evaluation["positive"]
            state.improvement_ai = state.improvement_final = evaluation["improvement"]
            state.source = "AI"
            state.ai_evaluated_at = state.evaluated_at = state.confirmed_at = utc_now()
        for evaluator in snapshot.evaluators.values():
            evaluator.closed = True
        snapshot.evaluations_published = True
        store.save_snapshot(snapshot)
        store.set_status(day, "COMPLETED")
    material = MaterialPreparer(store).prepare(
        run_id="demo-s04", start=date(2026, 10, 2), end=SOURCE_DAY,
        department_ids=[department_id])
    if not material.input.scope.complete:
        raise ValueError(f"Demo material incomplete: {material.input.scope.missing_sources}")
    return department_id, len(material.input.records)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--prepare-only", action="store_true")
    action.add_argument("--resume", action="store_true")
    action.add_argument("--resend-notification", action="store_true")
    args = parser.parse_args()
    os.chdir(ROOT)
    department_id, record_count = prepare()
    print(json.dumps({"material": "ready", "department_id": department_id,
                      "record_count": record_count, "state_dir": str(DEMO_ROOT)},
                     ensure_ascii=False), flush=True)
    if args.prepare_only:
        return 0

    load_env_file(ROOT / ".env")
    settings = replace(AppSettings.from_env(), state_dir=str(DEMO_ROOT),
                       workflow2_enabled=True, simulation_mode=True)
    if not settings.message_override_open_id.strip():
        parser.error("RECORDHUB_MESSAGE_OVERRIDE_OPEN_ID must be set for the demo")
    runtime = build_runtime(settings)
    workflow2 = runtime.workflow2
    assert workflow2 is not None
    workflow2.messages = DemoMessages(workflow2.messages)
    if args.resend_notification:
        run_id = str(uuid5(NAMESPACE_URL,
                           f"recordhub-workflow2:stage:{SCHEDULED_DAY}"))
        existing = workflow2.store.load(run_id)
        if existing is None or department_id not in existing.departments:
            parser.error("Start the demo cycle before resending its notification")
        organization = _organization()
        minister_id = organization.department_map()[department_id].minister_id
        minister = organization.person_map()[minister_id]
        form_url = workflow2._schedule("stage").options["form_url"]
        message = (f"{existing.start_date} 至 {existing.end_date} 周期分析待确认。\n"
                   f"请核对本部门分析并填写确认表：{form_url}")
        sent = workflow2.messages.send_text(
            minister.open_id, message,
            idempotency_key=f"demo-s04-resend:{run_id}:{uuid4()}")
        print(json.dumps({"resent_to": "RECORDHUB_MESSAGE_OVERRIDE_OPEN_ID",
                          "message_id": sent.get("message_id")},
                         ensure_ascii=False), flush=True)
        return 0
    if args.resume:
        run_id = str(uuid5(NAMESPACE_URL,
                           f"recordhub-workflow2:stage:{SCHEDULED_DAY}"))
        result = workflow2.resume(run_id)
    else:
        result = workflow2.start("stage", SCHEDULED_DAY)
    print(result.model_dump_json(indent=2), flush=True)
    return 0 if result.status not in {"FAILED", "BLOCKED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Clear only artifacts created by scripts/run_s04_demo_workflow.py.

The three synthetic daily input snapshots are preserved for replay.
Existing Feishu chat messages cannot be retracted.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from config import AppSettings, load_env_file, load_table_config
from service.runtime import build_runtime
from tool.bitable_fields import record_fields, scalar

STATE_DIR = ROOT / "data" / "state" / "s04_demo" / "workflow2"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    files = list(STATE_DIR.glob("*.json")) if STATE_DIR.exists() else []
    if not files:
        print("No S04 demo run state found.")
        return 0
    if len(files) != 1:
        parser.error("Expected exactly one S04 demo run state")
    state_path = files[0]
    run = json.loads(state_path.read_text(encoding="utf-8"))
    if run.get("kind") != "stage" or run.get("scheduled_date") != "2026-10-05":
        parser.error("Unexpected demo run identity; refusing to clear")
    run_id = run["run_id"]
    department_ids = set(run.get("departments", {}))
    if not department_ids:
        parser.error("Demo run has no department")

    load_env_file(ROOT / ".env")
    settings = AppSettings.from_env()
    runtime = build_runtime(settings)
    tables = load_table_config(settings.table_config_path).tables
    bitable = runtime.infrastructure.bitable
    wanted_tasks = {f"{run_id}:{department_id}" for department_id in department_ids}
    plans: dict[str, list[str]] = {}
    for name in ("stage_analysis", "stage_confirmation", "stage_report"):
        table = tables[name]
        task_field = table.fields["task_id"]
        valid_tasks = wanted_tasks | ({f"{run_id}:TEAM"} if name == "stage_report" else set())
        plans[name] = [str(row["record_id"]) for row in bitable.list_records(table.table_id)
                       if scalar(record_fields(row).get(task_field)) in valid_tasks]
    confirmation_ids = {draft.get("confirmation_record_id")
                        for draft in run["departments"].values()}
    confirmation_ids.discard(None)
    if confirmation_ids - set(plans["stage_confirmation"]):
        parser.error("Saved confirmation record is not among matching rows")
    urls = set(run.get("document_urls", {}).values())
    report_table = tables["reports"]
    url_field = report_table.fields["document_url"]
    plans["reports"] = [str(row["record_id"]) for row in
                        bitable.list_records(report_table.table_id)
                        if scalar(record_fields(row).get(url_field)) in urls]
    tokens = sorted(set(run.get("document_tokens", {}).values()))
    print(json.dumps({"run_id": run_id, "records": plans,
                      "document_tokens": tokens, "local_state": str(state_path),
                      "synthetic_daily_inputs_preserved": True},
                     ensure_ascii=False, indent=2), flush=True)
    if not args.execute:
        print("Preview only. Add --execute to clear these artifacts.")
        return 0
    for name, record_ids in plans.items():
        table_id = tables[name].table_id
        for record_id in record_ids:
            bitable.client.request(
                "DELETE", f"bitable/v1/apps/{settings.feishu.bitable_app_token}"
                f"/tables/{table_id}/records/{record_id}")
            print(f"deleted {name} {record_id}", flush=True)
    for token in tokens:
        bitable.client.request("DELETE", f"drive/v1/files/{token}",
                               query={"type": "docx"})
        print(f"deleted document {token}", flush=True)
    state_path.unlink()
    print(f"deleted local state {state_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

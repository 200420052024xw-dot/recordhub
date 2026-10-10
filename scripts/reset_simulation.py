"""Reset all simulation side effects before a clean re-run or teardown.

Deletes the generated cloud-document folders under the archive parent
(workflow1 daily date folders + the workflow2 folder) and clears every
simulation table (source tables persons/departments/logs plus the
Workflow1/Workflow2 output tables). Only the prompts/skill table is kept.

Usage:
    python scripts/reset_simulation.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from config import AppSettings, load_env_file, load_table_config
from tool.cloud_docs import CloudDocsService
from tool.feishu import BitableService, FeishuClient
from tool.http import UrllibTransport

ARCHIVE_FOLDER_NAMES = [
    "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03",
    "2026-10-04", "2026-10-05", "workflow2",
]

OUTPUT_TABLES = [
    "persons", "departments", "logs",
    "evaluations", "human_evaluations", "human_evaluations_backbone", "reports", "check_details",
    "stage_analysis", "stage_confirmation", "stage_report",
    "monthly_department_analysis", "monthly_department_confirmation",
    "monthly_department_report", "team_analysis",
]


def _delete_folder(client: FeishuClient, token: str) -> bool:
    try:
        client.request("DELETE", f"drive/v1/files/{token}", query={"type": "folder"})
        return True
    except Exception as exc:
        print(f"  folder delete failed: {exc}", flush=True)
        return False


def main() -> int:
    load_env_file()
    app = AppSettings.from_env()
    tables = load_table_config(app.table_config_path).tables
    client = FeishuClient(app.feishu, UrllibTransport(app.http_timeout_seconds))
    docs = CloudDocsService(client)
    bitable = BitableService(client, app.feishu.bitable_app_token)

    result: dict = {"deleted_folders": [], "cleared_tables": {}}

    if app.archive_parent_folder_token:
        existing = {item.get("name"): item for item in docs.list_files(
            app.archive_parent_folder_token)}
        for name in ARCHIVE_FOLDER_NAMES:
            item = existing.get(name)
            if item and item.get("type") == "folder":
                token = item.get("token")
                if token and _delete_folder(client, str(token)):
                    result["deleted_folders"].append(name)
                else:
                    result["deleted_folders"].append(f"{name}:FAILED")
    else:
        print("archive parent token missing; skipping doc cleanup", flush=True)

    app_token = app.feishu.bitable_app_token
    for name in OUTPUT_TABLES:
        if name not in tables:
            result["cleared_tables"][name] = "MISSING"
            continue
        table_id = tables[name].table_id
        records = bitable.list_records(table_id)
        ids = [str(record.get("record_id", "")) for record in records
               if record.get("record_id")]
        for offset in range(0, len(ids), 500):
            client.request(
                "POST",
                f"bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_delete",
                json_body={"records": list(ids[offset:offset + 500])},
            )
        result["cleared_tables"][name] = len(ids)

    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

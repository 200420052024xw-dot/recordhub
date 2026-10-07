"""Remove the simulated logs and any documents the workflows created.

Uses data/state/simulation_manifest.json as the source of truth for which
records and documents were inserted by the simulation scripts. Adds a
--hard-mode that also deletes all Feishu evaluation/report rows whose
业务编号/任务编号 prefixes match the simulated days (best effort: the
prefix is "YYYY-MM-DD:" which collides with non-simulated rows on the same
day — review the printed plan before passing --execute).

Usage:
    python scripts/cleanup_simulation_logs.py --preview
    python scripts/cleanup_simulation_logs.py --execute
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from config import AppSettings, load_env_file, load_table_config
from tool.bitable_fields import record_fields, scalar
from tool.feishu import BitableService, FeishuClient
from tool.http import UrllibTransport

MANIFEST_PATH = ROOT / "data" / "state" / "simulation_manifest.json"


def _iter_batches(ids: Iterable[str], size: int = 500):
    batch: list[str] = []
    for rid in ids:
        if not rid:
            continue
        batch.append(rid)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _find_simulated_rows(bitable: BitableService, table_id: str,
                          task_field: str, days: list[str]) -> list[str]:
    out: list[str] = []
    for record in bitable.list_records(table_id):
        value = scalar(record_fields(record).get(task_field))
        for day in days:
            if value.startswith(f"{day}:"):
                out.append(str(record.get("record_id", "")))
                break
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true",
                        help="actually delete records (default is preview)")
    parser.add_argument("--hard-mode", action="store_true",
                        help="also delete generated workflow1/workflow2 Feishu rows for these days")
    parser.add_argument("--reset-state", action="store_true",
                        help="remove local data/state/workflow1 and /state/workflow2 files")
    args = parser.parse_args()

    if not MANIFEST_PATH.exists():
        print("No simulation manifest; nothing to clean.")
        return 0
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    log_ids = [r.get("record_id", "") for r in manifest.get("records", [])]
    log_ids = [rid for rid in log_ids if rid]
    document_tokens = manifest.get("documents", []) or []
    days = list(manifest.get("days", []))

    load_env_file()
    app = AppSettings.from_env()
    tables = load_table_config(app.table_config_path)
    client = FeishuClient(app.feishu, UrllibTransport(app.http_timeout_seconds))
    bitable = BitableService(client, app.feishu.bitable_app_token)

    summary: dict = {
        "logs": len(log_ids),
        "documents": len(document_tokens),
        "days": days,
        "hard_mode_plans": {},
        "local_state": [],
    }

    if args.hard_mode:
        task_tables = ("evaluations", "human_evaluations", "stage_analysis",
                       "stage_confirmation", "stage_report",
                       "monthly_department_analysis",
                       "monthly_department_confirmation",
                       "monthly_department_report", "team_analysis", "reports",
                       "check_details")
        for name in task_tables:
            if name not in tables.tables:
                continue
            table = tables.tables[name]
            task_field = None
            for key in ("business_key", "task_id", "evaluation_id",
                        "evaluation_id", "title"):
                if key in table.fields:
                    task_field = table.fields[key]
                    break
            if task_field is None:
                continue
            ids = _find_simulated_rows(bitable, table.table_id, task_field, days)
            summary["hard_mode_plans"][name] = ids

    if args.reset_state:
        for sub in ("workflow1", "workflow2"):
            directory = ROOT / "data" / "state" / sub
            if directory.exists():
                for path in sorted(directory.glob("*.json")):
                    if path.stem in days:
                        summary["local_state"].append(str(path))

    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if not args.execute:
        print("Preview only. Add --execute to delete.")
        return 0

    for batch in _iter_batches(log_ids):
        client.request(
            "POST",
            f"bitable/v1/apps/{app.feishu.bitable_app_token}"
            f"/tables/{tables.tables['logs'].table_id}/records/batch_delete",
            json_body={"records": list(batch)},
        )
        print(f"deleted logs batch={len(batch)}", flush=True)

    if args.hard_mode:
        for name, ids in summary["hard_mode_plans"].items():
            if not ids:
                continue
            table = tables.tables[name]
            for batch in _iter_batches(ids):
                client.request(
                    "POST",
                    f"bitable/v1/apps/{app.feishu.bitable_app_token}"
                    f"/tables/{table.table_id}/records/batch_delete",
                    json_body={"records": list(batch)},
                )
                print(f"deleted {name} batch={len(batch)}", flush=True)

    for token in document_tokens:
        try:
            client.request("DELETE", f"drive/v1/files/{token}", query={"type": "docx"})
            print(f"deleted document {token}", flush=True)
        except Exception as exc:
            print(f"document delete failed {token}: {exc}", flush=True)

    if args.reset_state:
        for path in summary["local_state"]:
            try:
                os.remove(path)
                print(f"removed state {path}", flush=True)
            except OSError as exc:
                print(f"state remove failed {path}: {exc}", flush=True)

    MANIFEST_PATH.unlink(missing_ok=True)
    print("Manifest removed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
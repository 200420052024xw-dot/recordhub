"""Clear one Workflow1 day's generated artifacts, keeping source logs intact.

Preview:
    python scripts/clear_workflow1_day.py --date 2026-10-04
Execute:
    python scripts/clear_workflow1_day.py --date 2026-10-04 --execute

Completed deletions are journaled outside the Workflow1 run directory, so an
interrupted cleanup can resume with the same command. Feishu messages cannot be
retracted by this script.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from config import AppSettings, load_env_file, load_table_config
from service.runtime import build_runtime
from tool.bitable_fields import field_datetime, record_fields
from tool.errors import FeishuApiError

GENERATED_TABLES = ("evaluations", "reports", "check_details")


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _plan(state: dict, target_date: date) -> tuple[dict[str, list[str]], list[str]]:
    if state.get("run", {}).get("target_date") != target_date.isoformat():
        raise ValueError("State file date does not match --date")
    snapshot = state.get("snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("Workflow1 snapshot is missing")
    unknown = {entry["table_name"] for entry in state.get("external_records", {}).values()} - {
        "reports", "check_details"
    }
    if unknown:
        raise ValueError(f"Unknown generated table mappings: {sorted(unknown)}")
    records = {
        "evaluations": sorted({entry["record_id"] for entry in
                               snapshot.get("log_evaluations", {}).values()
                               if entry.get("record_id")}),
        "reports": sorted({entry["record_id"] for entry in
                           state.get("external_records", {}).values()
                           if entry["table_name"] == "reports"}),
        "check_details": sorted({entry["record_id"] for entry in
                                 state.get("external_records", {}).values()
                                 if entry["table_name"] == "check_details"}),
    }
    documents = sorted({entry["token"] for entry in
                        snapshot.get("cloud_objects", {}).values()})
    return records, documents


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--execute", action="store_true",
                        help="delete generated Feishu artifacts and local run state")
    parser.add_argument("--include-human-evaluations", action="store_true",
                        help="also delete human evaluation rows dated --date")
    args = parser.parse_args()

    load_env_file(ROOT / ".env")
    settings = AppSettings.from_env()
    state_root = Path(settings.state_dir)
    if not state_root.is_absolute():
        state_root = ROOT / state_root
    state_path = state_root / "workflow1" / f"{args.date}.json"
    if not state_path.exists():
        print(f"No Workflow1 state for {args.date}; nothing to clear")
        return 0
    state = json.loads(state_path.read_text(encoding="utf-8"))
    run_id = state["run"]["workflow_run_id"]
    journal_path = state_root / "reset" / "workflow1" / f"{args.date}.{run_id}.json"
    records, documents = _plan(state, args.date)
    summary = {"date": str(args.date), "run_id": state["run"]["workflow_run_id"],
               "records": {name: len(ids) for name, ids in records.items()},
               "documents": len(documents),
               "notifications_not_retracted": len(state.get("notifications", {})),
               "source_logs_preserved": True}
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if not args.execute:
        print("Preview only. Add --execute to clear these artifacts.")
        return 0

    tables = load_table_config(settings.table_config_path).tables
    runtime = build_runtime(settings)
    bitable = runtime.infrastructure.bitable
    client = bitable.client
    existing_journal = (json.loads(journal_path.read_text(encoding="utf-8"))
                        if journal_path.exists() else None)
    if existing_journal and "human_evaluations" in existing_journal["manifest"]["records"]:
        records["human_evaluations"] = existing_journal["manifest"]["records"]["human_evaluations"]
    elif args.include_human_evaluations:
        table = tables["human_evaluations"]
        field = table.fields["evaluated_at"]
        records["human_evaluations"] = sorted(
            str(row["record_id"]) for row in bitable.list_records(table.table_id)
            if field_datetime(record_fields(row).get(field)).date() == args.date
        )

    manifest = {"run_id": state["run"]["workflow_run_id"],
                "records": records, "documents": documents}
    if existing_journal:
        journal = existing_journal
        if journal.get("manifest") != manifest:
            raise ValueError(f"Cleanup journal belongs to another run: {journal_path}")
    else:
        journal = {"manifest": manifest, "done": []}
        _write_json(journal_path, journal)

    done = set(journal["done"])
    for name, ids in records.items():
        table_id = tables[name].table_id
        live_ids = {str(row["record_id"]) for row in bitable.list_records(table_id)}
        for record_id in ids:
            key = f"{name}:{record_id}"
            if key in done:
                continue
            if record_id in live_ids:
                client.request("DELETE", f"bitable/v1/apps/{settings.feishu.bitable_app_token}"
                               f"/tables/{table_id}/records/{record_id}")
                print(f"deleted {key}", flush=True)
            else:
                print(f"already absent {key}", flush=True)
            done.add(key)
            journal["done"] = sorted(done)
            _write_json(journal_path, journal)

    for token in documents:
        key = f"docx:{token}"
        if key in done:
            continue
        try:
            client.request("DELETE", f"drive/v1/files/{token}", query={"type": "docx"})
            print(f"deleted {key}", flush=True)
        except FeishuApiError as exc:
            if exc.status_code != 404:
                raise
            print(f"already absent {key}", flush=True)
        done.add(key)
        journal["done"] = sorted(done)
        _write_json(journal_path, journal)

    state_path.unlink()
    print(f"deleted local state {state_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

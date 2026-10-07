"""Delete stale AI evaluation rows for the simulated date window.

Used before re-running Workflow1 against the freshly generated 100 simulation
logs. Keeps the new logs intact. Targets the AI evaluation table's business
key prefix (YYYY-MM-DD:) for the days listed in the manifest.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from config import AppSettings, load_env_file, load_table_config
from tool.feishu import BitableService, FeishuClient
from tool.http import UrllibTransport
from tool.bitable_fields import record_fields, scalar

MANIFEST_PATH = ROOT / "data" / "state" / "simulation_manifest.json"
W1_STATE_DIR = ROOT / "data" / "state" / "workflow1"


def main() -> int:
    if not MANIFEST_PATH.exists():
        print("No simulation manifest; nothing to do.")
        return 0
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    days = list(manifest.get("days", []))
    if not days:
        print("Manifest has no days.")
        return 0
    print(f"Simulated days: {days}")

    load_env_file()
    app = AppSettings.from_env()
    tables = load_table_config(app.table_config_path)
    client = FeishuClient(app.feishu, UrllibTransport(app.http_timeout_seconds))
    bitable = BitableService(client, app.feishu.bitable_app_token)

    table_id = tables.tables["evaluations"].table_id
    business_field = tables.tables["evaluations"].fields.get("business_key")

    matched: list[dict] = []
    for record in bitable.list_records(table_id):
        v = scalar(record_fields(record).get(business_field))
        rid = str(record.get("record_id", ""))
        if not rid:
            continue
        for day in days:
            if v.startswith(f"{day}:"):
                matched.append({"record_id": rid, "key_prefix": v[:35]})
                break

    print(f"Matched {len(matched)} stale evaluation rows.")

    deleted = 0
    for offset in range(0, len(matched), 500):
        batch = [m["record_id"] for m in matched[offset:offset + 500]]
        client.request(
            "POST",
            f"bitable/v1/apps/{app.feishu.bitable_app_token}"
            f"/tables/{table_id}/records/batch_delete",
            json_body={"records": list(batch)},
        )
        deleted += len(batch)
        print(f"  deleted batch {offset // 500 + 1}: {len(batch)} rows")

    removed_state: list[str] = []
    if W1_STATE_DIR.exists():
        for path in sorted(W1_STATE_DIR.glob("*.json")):
            if path.stem in days:
                path.unlink(missing_ok=True)
                removed_state.append(str(path))
    if removed_state:
        print(f"Removed {len(removed_state)} local workflow1 state files.")

    print(json.dumps({
        "matched": len(matched),
        "deleted": deleted,
        "removed_state": removed_state,
        "days": days,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
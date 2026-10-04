"""Backfill the two composed-log text columns for an existing Workflow1 day."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from config import AppSettings, load_env_file
from service.runtime import build_runtime


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date", type=date.fromisoformat)
    args = parser.parse_args()

    load_env_file(ROOT / ".env")
    runtime = build_runtime(AppSettings.from_env())
    binding = runtime.workflows["workflow1_daily"]
    snapshot = binding.store.load_snapshot(args.date)
    if snapshot is None:
        parser.error(f"Workflow1 snapshot not found: {args.date}")

    logs, _ = binding.workflow.log_repository.get_logs_by_date(
        args.date, organization=snapshot.organization)
    live_by_id = {log.source_record_id: log for log in logs}
    current = []
    for log in snapshot.logs:
        live = live_by_id.get(log.source_record_id)
        if live is None or live.content() != log.content():
            raise ValueError(f"Source log changed since snapshot: {log.log_id}")
        current.append(live)

    source_count = binding.workflow.log_repository.backfill_full_logs(current)
    evaluation_count = binding.workflow.ai_evaluations.backfill_source_logs(snapshot)
    print(f"date={args.date} source_logs_updated={source_count} "
          f"ai_evaluations_updated={evaluation_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

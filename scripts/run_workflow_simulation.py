"""Run workflow1 and workflow2 against the simulated logs.

This wraps main.py to exercise every part of the daily and periodic
pipelines. Pass --no-finalize to leave Workflow1 evaluations waiting for
human review; otherwise finalize-confirmations runs after each Workflow1
day so the workflow reaches the COMPLETED status.

Usage:
    python scripts/run_workflow_simulation.py --workflow1
    python scripts/run_workflow_simulation.py --stage 2026-10-05
    python scripts/run_workflow_simulation.py --monthly 2026-10-01
    python scripts/run_workflow_simulation.py --weekly 2026-10-05
    python scripts/run_workflow_simulation.py --all
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def _run(args: list[str]) -> dict:
    print(f"\n=== {' '.join(args)} ===", flush=True)
    proc = subprocess.run(args, cwd=str(ROOT), capture_output=True,
                         text=True, encoding="utf-8")
    print("stdout:", proc.stdout, flush=True)
    if proc.stderr:
        print("stderr:", proc.stderr[-2000:], flush=True)
    if proc.returncode != 0:
        print(f"!! exit code {proc.returncode}", flush=True)
        return {"status": "failed", "exit_code": proc.returncode,
                "stderr": proc.stderr[-2000:], "stdout": proc.stdout}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "unknown", "stdout": proc.stdout}


def _workflow1_dates() -> list[date]:
    return [date(2026, 10, d) for d in (1, 2, 3, 4, 5)]


def run_workflow1(args) -> dict:
    if not PYTHON.exists():
        return {"status": "skipped", "reason": f"{PYTHON} not found"}
    results = []
    for target in _workflow1_dates():
        out = _run([str(PYTHON), "main.py", "run-workflow",
                    "--date", target.isoformat()])
        results.append({"date": target.isoformat(), **out})
    if args.finalize:
        out = _run([str(PYTHON), "main.py", "finalize-confirmations"])
        results.append({"finalize": out})
    return {"phase": "workflow1", "days": results}


def run_stage(scheduled: date) -> dict:
    out = _run([str(PYTHON), "main.py", "run-workflow2",
                "--kind", "stage", "--date", scheduled.isoformat()])
    return {"phase": "stage", "scheduled": scheduled.isoformat(), **out}


def run_monthly(scheduled: date) -> dict:
    out = _run([str(PYTHON), "main.py", "run-workflow2",
                "--kind", "monthly", "--date", scheduled.isoformat()])
    return {"phase": "monthly", "scheduled": scheduled.isoformat(), **out}


def run_weekly(scheduled: date) -> dict:
    out = _run([str(PYTHON), "main.py", "run-workflow2",
                "--kind", "weekly", "--date", scheduled.isoformat()])
    return {"phase": "weekly", "scheduled": scheduled.isoformat(), **out}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow1", action="store_true")
    parser.add_argument("--stage", type=date.fromisoformat)
    parser.add_argument("--monthly", type=date.fromisoformat)
    parser.add_argument("--weekly", type=date.fromisoformat)
    parser.add_argument("--all", action="store_true",
                        help="跑全部:workflow1 + stage(2026-10-05) + monthly(2026-10-01) + weekly(2026-10-05)")
    parser.add_argument("--no-finalize", dest="finalize", action="store_false")
    args = parser.parse_args()
    if not (args.workflow1 or args.stage or args.monthly or args.weekly or args.all):
        parser.error("Specify at least one of --workflow1/--stage/--monthly/--weekly/--all")
    summary: dict = {"python": str(PYTHON)}
    if args.workflow1 or args.all:
        summary["workflow1"] = run_workflow1(args)
    if args.all or args.stage:
        summary["stage"] = run_stage(args.stage or date(2026, 10, 5))
    if args.all or args.monthly:
        summary["monthly"] = run_monthly(args.monthly or date(2026, 10, 1))
    if args.all or args.weekly:
        summary["weekly"] = run_weekly(args.weekly or date(2026, 10, 5))
    print("\n=== summary ===", flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
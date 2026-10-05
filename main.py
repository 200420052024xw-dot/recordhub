from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

# Keep direct `python main.py` execution aligned with the src-layout package.
_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from config import AppSettings, load_env_file, load_schedule_config, load_table_config


def check_config(settings: AppSettings) -> int:
    schedules = load_schedule_config(settings.schedule_config_path)
    from workflow1.settings import W1Settings

    w1 = W1Settings.from_env()
    missing = settings.missing_variables()
    table_config_error = ""
    workflow2_missing: list[str] = []
    try:
        tables = load_table_config(settings.table_config_path)
        required = ("stage_analysis", "stage_confirmation", "stage_report",
                    "monthly_department_analysis", "monthly_department_confirmation",
                    "monthly_department_report", "team_analysis")
        workflow2_missing = [name for name in required
                             if name not in tables.tables or not tables.tables[name].table_id]
        if not schedules.workflows["s05_people_suggestions"].options.get("form_url"):
            workflow2_missing.append("monthly_form_url")
    except ValueError as exc:
        table_config_error = str(exc)
    result = {
        "status": "ready" if not missing and not table_config_error and
        (not settings.workflow2_enabled or not workflow2_missing) else "incomplete",
        "missing_variables": missing,
        "table_config_error": table_config_error or None,
        "workflow2_missing": workflow2_missing,
        "feishu_base_url": settings.feishu.base_url,
        "deepseek_base_url": settings.deepseek.base_url,
        "deepseek_model": settings.deepseek.model,
        "timezone": schedules.timezone,
        "state_dir": settings.state_dir,
        "table_config": settings.table_config_path,
        "scheduler_enabled": settings.scheduler_enabled,
        "workflow2_enabled": settings.workflow2_enabled,
        "enabled_schedules": [
            name for name, item in schedules.workflows.items() if item.enabled
        ],
        "workflow1": {
            "auto_advance_at": w1.auto_advance_at or None,
            "confirmation_webhook_configured": bool(w1.confirmation_webhook_token),
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not missing and not table_config_error and (
        not settings.workflow2_enabled or not workflow2_missing) else 1


def main() -> int:
    load_env_file()
    settings = AppSettings.from_env()
    parser = argparse.ArgumentParser(description="RecordHub Workflow1 and Workflow2 service")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("check-config")

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=8000)

    run_parser = subparsers.add_parser("run-workflow")
    run_parser.add_argument("--date", required=True, type=date.fromisoformat)
    resume_parser = subparsers.add_parser("resume-workflow")
    resume_parser.add_argument("--date", required=True, type=date.fromisoformat)
    finalize_parser = subparsers.add_parser("finalize-confirmations")
    finalize_parser.add_argument("--date", type=date.fromisoformat)

    refresh_parser = subparsers.add_parser("refresh-cache")
    refresh_parser.add_argument(
        "cache", choices=["organization", "all"]
    )
    workflow2_parser = subparsers.add_parser("run-workflow2")
    workflow2_parser.add_argument("--kind", required=True, choices=["stage", "monthly", "weekly"])
    workflow2_parser.add_argument("--date", required=True, type=date.fromisoformat)
    workflow2_resume = subparsers.add_parser("resume-workflow2")
    workflow2_resume.add_argument("--run-id", required=True)
    args = parser.parse_args()
    command = args.command or "check-config"
    if command == "check-config":
        return check_config(settings)
    if command == "serve":
        # Module loggers otherwise fall through to the WARNING lastResort
        # handler, hiding confirmation/event-stream observability.
        logging.basicConfig(level=logging.INFO)
        import uvicorn

        from service.api import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return 0

    from service.runtime import build_runtime

    runtime = build_runtime(settings)
    workflow1 = runtime.workflows["workflow1_daily"]
    if command == "run-workflow2":
        result = runtime.workflow2.start(args.kind, args.date)
    elif command == "resume-workflow2":
        result = runtime.workflow2.resume(args.run_id)
    elif command == "run-workflow":
        result = workflow1.workflow.start(args.date)
    elif command == "resume-workflow":
        result = workflow1.workflow.resume(args.date)
    elif command == "finalize-confirmations":
        dates = (
            [args.date]
            if args.date
            else [run.target_date for run in workflow1.store.list_incomplete_workflows()]
        )
        results = [
            workflow1.workflow.finalize_pending_confirmations(one) for one in dates
        ]
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    elif command == "refresh-cache":
        payload: dict[str, object] = {}
        if args.cache in {"organization", "all"}:
            organization = runtime.organization_cache.refresh()
            payload["organization"] = {
                "persons": len(organization.persons),
                "departments": len(organization.departments),
                "anomalies": organization.anomalies,
            }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    else:
        parser.error(f"Unknown command: {command}")
        return 2
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

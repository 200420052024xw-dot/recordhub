"""Offline deployment checks. Never builds a runtime or configures alerts."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


def inspect_deployment(*, production: bool = False, check_port: bool = False,
                       port: int = 8100) -> dict:
    from config import AppSettings, load_env_file
    from main import check_config

    errors: list[str] = []
    warnings: list[str] = []
    load_env_file()
    try:
        settings = AppSettings.from_env()
        # Reuse the application's configuration checks without logging setup,
        # alerts, network calls, scheduler or organization-cache refresh.
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = check_config(settings)
        config = json.loads(output.getvalue())
        if result:
            errors.extend("missing: " + key for key in config["missing_variables"])
            if config["table_config_error"]:
                errors.append("table configuration is invalid; run main.py check-config")
            if settings.workflow2_enabled:
                errors.extend("workflow2 missing: " + key for key in config["workflow2_missing"])
        settings.validate()
    except Exception as exc:
        # Exception messages from parsers can include source values; never echo
        # credentials or .env contents into a shareable deployment report.
        errors.append(f"configuration validation failed ({type(exc).__name__})")
        return {"status": "blocked", "errors": errors, "warnings": warnings}

    from service.workbuddy_review.settings import ReviewSettings
    from config import load_schedule_config
    try:
        schedules = load_schedule_config(settings.schedule_config_path)
        ReviewSettings.from_schedule(schedules)
    except Exception as exc:
        errors.append(f"review schedule is invalid ({type(exc).__name__})")

    for name in ["S01", "S04", "S05", "S06", "S07", "S08", "S09", "S10"]:
        if not (ROOT / "prompts" / f"{name}.txt").is_file():
            errors.append(f"missing prompt: {name}.txt")

    for name, directory in [("RECORDHUB_STATE_DIR", settings.state_dir),
                             ("RECORDHUB_LOG_DIR", settings.log_dir)]:
        try:
            # Require provisioned directories and exercise real permissions.
            with tempfile.TemporaryFile(dir=directory) as handle:
                handle.write(b"recordhub-deployment-check")
                handle.flush()
        except OSError:
            errors.append(f"{name}: directory must exist and be writable")

    tokens = {name: os.getenv(name, "") for name in (
        "RECORDHUB_ADMIN_TOKEN", "RECORDHUB_WORKBUDDY_TOKEN",
        "RECORDHUB_QUERY_TOKEN", "RECORDHUB_REVIEW_TOKEN",
        "RECORDHUB_CONFIRMATION_WEBHOOK_TOKEN")}
    for name, value in tokens.items():
        if not value:
            warnings.append(f"{name}: endpoint disabled")
        elif len(value) < 32 or value != value.strip():
            (errors if production else warnings).append(
                f"{name}: use at least 32 random characters without surrounding whitespace")
    if not settings.admin_open_id.strip():
        (errors if production else warnings).append("RECORDHUB_ADMIN_OPEN_ID: alerts have no recipient")
    if production and not settings.admin_token:
        errors.append("RECORDHUB_ADMIN_TOKEN: required for production administration")
    if settings.simulation_mode:
        (errors if production else warnings).append("simulation mode is enabled")
    if settings.message_override_open_id:
        warnings.append("message override is set; verify production recipient settings")
    if not settings.scheduler_enabled:
        warnings.append("scheduler is disabled; business jobs will not run automatically")
    if not settings.event_stream_enabled:
        warnings.append("Feishu event stream is disabled")
    if settings.release == "unknown":
        warnings.append("RECORDHUB_RELEASE: set a release identifier before deployment")
    if production and sys.platform != "linux":
        warnings.append("target Linux environment still requires validation")

    if check_port:
        try:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", port))
        except OSError:
            errors.append(f"cannot bind 127.0.0.1:{port}; run before starting the service")
    return {"status": "blocked" if errors else "passed", "port": port,
            "production": production, "errors": errors, "warnings": warnings,
            "scope": "offline only; no remote API, DNS, TLS or target-server validation"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--check-port", action="store_true")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    os.chdir(ROOT)
    report = inspect_deployment(production=args.production,
                                check_port=args.check_port, port=args.port)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

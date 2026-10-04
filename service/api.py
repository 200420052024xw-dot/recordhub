from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Annotated, AsyncIterator
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Header, HTTPException

from schema import ConfirmationRequest
from service.event_stream import ConfirmationEventStream
from service.runtime import Runtime, build_runtime
from config import AppSettings, load_env_file, load_schedule_config

logger = logging.getLogger(__name__)


def _bearer_token(authorization: str | None) -> str:
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        return ""
    return authorization[len(prefix) :]


def _require_token(expected: str, authorization: str | None) -> None:
    if not expected:
        raise HTTPException(status_code=503, detail="Endpoint token is not configured")
    if not hmac.compare_digest(_bearer_token(authorization), expected):
        raise HTTPException(status_code=401, detail="Invalid bearer token")


def create_app(
    settings: AppSettings | None = None, runtime: Runtime | None = None
) -> FastAPI:
    if settings is None:
        load_env_file()
        settings = AppSettings.from_env()
    active_runtime = runtime or build_runtime(settings)
    schedules = load_schedule_config(settings.schedule_config_path)
    app = FastAPI(
        title="RecordHub Workflow1",
        version="0.2.0",
        lifespan=_make_lifespan(active_runtime, settings, schedules),
    )
    app.state.runtime = active_runtime
    _register_public_routes(app, active_runtime, settings)
    _register_admin_routes(app, active_runtime, settings)
    return app


def _make_lifespan(runtime, settings, schedules):
    scheduler: BackgroundScheduler | None = None
    stream: ConfirmationEventStream | None = None

    def run_daily() -> None:
        schedule = schedules.workflows["workflow1_daily"]
        days_ago = int(schedule.options.get("material_days_ago", 1))
        today = datetime.now(ZoneInfo(schedules.timezone)).date()
        runtime.workflow.start(today - timedelta(days=days_ago))

    def cleanup() -> None:
        today = datetime.now(ZoneInfo(schedules.timezone)).date()
        runtime.store.cleanup_completed(settings.snapshot_retention_days, today=today)

    def auto_advance() -> None:
        for run in runtime.store.list_incomplete_workflows():
            result = runtime.workflow.finalize_pending_confirmations(run.target_date)
            logger.info("auto_advance_result", extra={"result": result})

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal scheduler, stream
        runtime.organization_cache.refresh()
        runtime.workflow.recover_incomplete()
        if settings.event_stream_enabled:
            stream = ConfirmationEventStream(
                workflow=runtime.workflow,
                bitable=runtime.infrastructure.bitable,
                human_evaluations=runtime.human_evaluations,
                organization_cache=runtime.organization_cache,
                settings=settings,
            )
            stream.start()
        if settings.scheduler_enabled:
            scheduler = _build_scheduler(
                schedules, run_daily, cleanup, auto_advance, settings.auto_advance_at
            )
            scheduler.start()
        try:
            yield
        finally:
            if stream:
                stream.stop()
            if scheduler:
                scheduler.shutdown(wait=False)

    return lifespan


def _build_scheduler(schedules, run_daily, cleanup, auto_advance, auto_advance_at):
    schedule = schedules.workflows["workflow1_daily"]
    hour, minute = (int(value) for value in schedule.time.split(":", 1))
    scheduler = BackgroundScheduler(timezone=schedules.timezone)
    scheduler.add_job(
        run_daily,
        "cron",
        hour=hour,
        minute=minute,
        id="workflow1_daily",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    if auto_advance_at:
        deadline_hour, deadline_minute = (
            int(value) for value in auto_advance_at.split(":", 1)
        )
        scheduler.add_job(
            auto_advance,
            "cron",
            hour=deadline_hour,
            minute=deadline_minute,
            id="confirmation_auto_advance",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    scheduler.add_job(
        cleanup,
        "cron",
        hour=3,
        minute=30,
        id="snapshot_cleanup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return scheduler


def _register_public_routes(app, runtime, settings):
    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        return {"status": "ready"}

    @app.post("/webhooks/feishu/confirmations")
    def confirmation(
        request: ConfirmationRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        _require_token(settings.confirmation_webhook_token, authorization)
        try:
            return runtime.workflow.handle_confirmation(request)
        except Exception as exc:
            logger.warning("confirmation_rejected", extra={"error": str(exc)})
            raise HTTPException(status_code=400, detail=str(exc)) from exc


def _register_admin_routes(app, runtime, settings):
    def require_admin(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        _require_token(settings.admin_token, authorization)

    @app.post("/admin/workflows/daily/{target_date}", dependencies=[Depends(require_admin)])
    def run_workflow(target_date: date) -> dict:
        return runtime.workflow.start(target_date).model_dump(mode="json")

    @app.post(
        "/admin/workflows/{target_date}/resume", dependencies=[Depends(require_admin)]
    )
    def resume_workflow(target_date: date) -> dict:
        return runtime.workflow.resume(target_date).model_dump(mode="json")

    @app.post(
        "/admin/cache/organization/refresh", dependencies=[Depends(require_admin)]
    )
    def refresh_organization() -> dict:
        organization = runtime.organization_cache.refresh()
        return {
            "persons": len(organization.persons),
            "departments": len(organization.departments),
            "anomalies": organization.anomalies,
        }




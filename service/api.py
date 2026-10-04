from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Annotated, AsyncIterator
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Header, HTTPException

from schema import (
    ConfirmationRequest,
    Organization,
    PersonCreateRequest,
    PersonUpdateRequest,
)
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
        try:
            runtime.organization_cache.refresh()
        except Exception:
            # Restart reads the Feishu tables once; if that fails, keep the
            # persisted cache as the runtime master data instead of dying.
            if runtime.store.load_cache("organization") is None:
                raise
            logger.exception(
                "organization_refresh_failed_using_persisted_cache"
            )
        runtime.workflow.recover_incomplete()
        if settings.event_stream_enabled:
            stream = ConfirmationEventStream(
                workflow=runtime.workflow,
                bitable=runtime.infrastructure.bitable,
                human_evaluations=runtime.human_evaluations,
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
        """重评接口：只重跑标记为失败或尚未处理的日志，已有结果的不动。"""
        return runtime.workflow.resume(target_date).model_dump(mode="json")

    @app.get(
        "/admin/workflows/{target_date}/issues", dependencies=[Depends(require_admin)]
    )
    def workflow_issues(target_date: date) -> dict:
        snapshot = runtime.store.load_snapshot(target_date)
        if snapshot is None:
            raise HTTPException(status_code=404, detail=f"{target_date} 没有当日快照")
        return {"target_date": target_date.isoformat(), "issues": snapshot.issues}

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

    @app.get("/admin/organization/persons", dependencies=[Depends(require_admin)])
    def list_persons() -> dict:
        return _organization_payload(runtime.organization_cache.get())

    @app.post(
        "/admin/organization/persons", status_code=201,
        dependencies=[Depends(require_admin)],
    )
    def add_person(request: PersonCreateRequest) -> dict:
        try:
            organization = runtime.organization_cache.add_person(request.to_person())
        except ValueError as exc:
            _raise_organization_error(exc)
        return _organization_payload(organization)

    @app.put(
        "/admin/organization/persons/{person_id}",
        dependencies=[Depends(require_admin)],
    )
    def update_person(person_id: str, request: PersonUpdateRequest) -> dict:
        try:
            organization = runtime.organization_cache.update_person(
                person_id, request.model_dump(exclude_unset=True)
            )
        except (ValueError, LookupError) as exc:
            _raise_organization_error(exc)
        return _organization_payload(organization)

    @app.delete(
        "/admin/organization/persons/{person_id}",
        dependencies=[Depends(require_admin)],
    )
    def delete_person(person_id: str) -> dict:
        try:
            organization = runtime.organization_cache.delete_person(person_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _organization_payload(organization)


def _organization_payload(organization: Organization) -> dict:
    return {
        "team_leader_id": organization.team_leader_id,
        "anomalies": organization.anomalies,
        "persons": [person.model_dump(mode="json") for person in organization.persons],
        "departments": [
            department.model_dump(mode="json")
            for department in organization.departments
        ],
    }


def _raise_organization_error(exc: Exception) -> None:
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if "已存在" in str(exc):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc




"""HTTP app, lifecycle and scheduling for registered workflows."""

from __future__ import annotations

import logging
import hmac
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Annotated, Any, AsyncIterator, Literal
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from config import AppSettings, load_env_file, load_schedule_config
from config.schedules import ScheduleConfig, WorkflowSchedule
from schema import Organization, PersonCreateRequest, PersonUpdateRequest
from service.bitable_events import BitableEventStream
from service.runtime import Runtime, WorkflowBinding, build_runtime, capture_notification_backlog
from workflow1.models import (
    ConfirmationRequest,
    WorkBuddyIdentityRequest,
    WorkBuddySubmitRequest,
)
from workflow1.workbuddy_inbox import LogSubmitService
from tool.diagnostics import error_summary

logger = logging.getLogger(__name__)


class NotificationSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dates: list[date] = Field(min_length=1, max_length=31)
    workflow: Literal["all", "workflow1", "workflow2"] = "all"
    message_type: Literal["all", "review", "report"] = "all"
    kind: Literal["all", "stage", "monthly", "weekly"] = "all"
    confirm_unsent: bool = Field(default=False, strict=True)


def _require_token(expected: str, authorization: str | None) -> None:
    if not expected:
        raise HTTPException(status_code=503, detail="Endpoint token is not configured")
    prefix = "Bearer "
    supplied = authorization[len(prefix):] if authorization and authorization.startswith(prefix) else ""
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid bearer token")


def create_app(
    settings: AppSettings | None = None, runtime: Runtime | None = None
) -> FastAPI:
    if settings is None:
        load_env_file()
        settings = AppSettings.from_env()
    if runtime is None:
        from service.alerts import configure_admin_alerts
        configure_admin_alerts(settings)
    active_runtime = runtime or build_runtime(settings)
    schedules = load_schedule_config(settings.schedule_config_path)
    app = FastAPI(
        title="RecordHub",
        version="0.2.0",
        lifespan=_make_lifespan(active_runtime, settings, schedules),
    )
    app.state.runtime = active_runtime

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        logger.error("http_request_failed method=%s path=%s error=%s",
                     request.method, request.url.path, error_summary(exc),
                     exc_info=(type(exc), exc, exc.__traceback__))
        return JSONResponse(status_code=500, content={"detail": "Internal server error; see service logs"})

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        return {"status": "ready"}

    _register_routes(app, active_runtime, settings)
    return app


def _register_routes(app: FastAPI, runtime: Runtime, settings: AppSettings) -> None:
    """Keep the public and admin HTTP API in one place."""
    workflow1 = runtime.workflows["workflow1_daily"]

    def require_admin(authorization: Annotated[str | None, Header()] = None) -> None:
        _require_token(settings.admin_token, authorization)

    @app.post("/webhooks/feishu/confirmations")
    def confirmation(
        request: ConfirmationRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict:
        _require_token(workflow1.confirmation_webhook_token, authorization)
        try:
            result = workflow1.workflow.handle_confirmation(request)
        except Exception as exc:
            logger.exception("confirmation_rejected record_id=%s error=%s",
                           request.record_id, exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        logger.info("confirmation_received record_id=%s status=%s",
                    request.record_id, result.get("status"))
        return result

    @app.post("/admin/workflows/daily/{target_date}", dependencies=[Depends(require_admin)])
    def run_workflow(target_date: date) -> dict:
        return workflow1.workflow.start(target_date).model_dump(mode="json")

    @app.post(
        "/admin/workflows/{target_date}/resume", dependencies=[Depends(require_admin)]
    )
    def resume_workflow(target_date: date) -> dict:
        return workflow1.workflow.resume(target_date).model_dump(mode="json")

    @app.get(
        "/admin/workflows/{target_date}/issues", dependencies=[Depends(require_admin)]
    )
    def workflow_issues(target_date: date) -> dict:
        snapshot = workflow1.store.load_snapshot(target_date)
        if snapshot is None:
            raise HTTPException(status_code=404, detail=f"{target_date} 没有当日快照")
        return {"target_date": target_date.isoformat(), "issues": snapshot.issues}

    @app.post("/admin/notifications/send", dependencies=[Depends(require_admin)])
    def send_notifications(request: NotificationSendRequest) -> dict:
        results = []
        logger.info("admin_notification_send_requested dates=%s workflow=%s type=%s kind=%s confirm_unsent=%s",
                    request.dates, request.workflow, request.message_type, request.kind, request.confirm_unsent)
        for target_date in dict.fromkeys(request.dates):
            for name in ("workflow1", "workflow2"):
                if request.workflow not in {"all", name}:
                    continue
                workflow = workflow1.workflow if name == "workflow1" else runtime.workflow2
                if workflow is None:
                    results.append({"workflow": name, "target_date": str(target_date), "status": "unavailable"})
                    continue
                try:
                    if name == "workflow1":
                        result = workflow.send_notifications(target_date, request.message_type,
                                                             confirm_unsent=request.confirm_unsent)
                    else:
                        result = workflow.send_notifications(target_date, request.message_type, request.kind,
                                                             confirm_unsent=request.confirm_unsent)
                    results.append(result)
                except LookupError as exc:
                    policy = getattr(workflow1.workflow, "notifications", None)
                    replayed = (policy.replay(target_date, runtime.infrastructure.messages,
                                             workflow=name, message_type=request.message_type,
                                             kind=request.kind, confirm_unsent=request.confirm_unsent) if policy else None)
                    results.append(replayed or {"workflow": name, "target_date": str(target_date),
                                                "status": "not_found", "error": str(exc)})
                except Exception as exc:
                    logger.exception("admin_notification_send_failed workflow=%s date=%s", name, target_date)
                    results.append({"workflow": name, "target_date": str(target_date),
                                    "status": "failed", "error": error_summary(exc)})
        summary: dict[str, int] = {}
        for result in results:
            for item in result.get("messages", [result]):
                status = item.get("status", "unknown")
                summary[status] = summary.get(status, 0) + 1
        logger.info("admin_notification_send_finished summary=%s", summary)
        return {"results": results, "summary": summary}

    @app.get("/admin/notifications/{target_date}", dependencies=[Depends(require_admin)])
    def notification_status(target_date: date) -> dict:
        policy = getattr(workflow1.workflow, "notifications", None)
        return {"target_date": str(target_date),
                "messages": policy.for_date(target_date) if policy else []}

    @app.post("/admin/cache/organization/refresh", dependencies=[Depends(require_admin)])
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


    @app.post("/admin/workflow2/{kind}/{scheduled_date}", dependencies=[Depends(require_admin)])
    def start_workflow2(kind: str, scheduled_date: date) -> dict:
        if runtime.workflow2 is None:
            raise HTTPException(status_code=503, detail="Workflow2 is unavailable")
        try:
            return runtime.workflow2.start(kind, scheduled_date).model_dump(mode="json")
        except Exception as exc:
            logger.exception("workflow2_admin_start_failed kind=%s date=%s", kind, scheduled_date)
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/admin/workflow2/runs/{run_id}/resume", dependencies=[Depends(require_admin)])
    def resume_workflow2(run_id: str) -> dict:
        try:
            return runtime.workflow2.resume(run_id).model_dump(mode="json")
        except Exception as exc:
            logger.exception("workflow2_admin_resume_failed run_id=%s", run_id)
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/admin/workflow2/runs/{run_id}", dependencies=[Depends(require_admin)])
    def get_workflow2(run_id: str) -> dict:
        run = runtime.workflow2.store.load(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Workflow2 run not found")
        return run.model_dump(mode="json")

    # 对话入口(WorkBuddy)专用:独立 token,拿不到 admin 权限。
    def require_workbuddy(
        authorization: Annotated[str | None, Header()] = None
    ) -> None:
        _require_token(settings.workbuddy_token, authorization)

    def workbuddy_service() -> LogSubmitService:
        if runtime.workbuddy is None:
            raise HTTPException(
                status_code=503, detail="WorkBuddy endpoint is not configured")
        return runtime.workbuddy

    @app.post("/workbuddy/v1/work-logs/identity",
              dependencies=[Depends(require_workbuddy)])
    def workbuddy_identity(request: WorkBuddyIdentityRequest) -> dict:
        return workbuddy_service().resolve(request.name)

    @app.post("/workbuddy/v1/work-logs",
              dependencies=[Depends(require_workbuddy)])
    def workbuddy_submit(request: WorkBuddySubmitRequest) -> dict:
        return workbuddy_service().submit(request)


def _organization_payload(organization: Organization) -> dict:
    return {
        "team_leader_id": organization.team_leader_id,
        "anomalies": organization.anomalies,
        "persons": [person.model_dump(mode="json") for person in organization.persons],
        "departments": [department.model_dump(mode="json") for department in organization.departments],
    }


def _raise_organization_error(exc: Exception) -> None:
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if "已存在" in str(exc):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


def _event_handlers(runtime: Runtime) -> dict[str, Callable[[str], Any]]:
    handlers: dict[str, Callable[[str], Any]] = {}
    for binding in runtime.workflows.values():
        for table_id, handler in binding.record_handlers.items():
            if table_id in handlers:
                raise ValueError(f"Duplicate event handler for table {table_id}")
            handlers[table_id] = handler
    if runtime.settings.workflow2_enabled and runtime.workflow2 is not None:
        for name in ("stage_confirmation", "monthly_department_confirmation"):
            table_id = runtime.workflow2.tables.table(name).table_id
            if table_id in handlers:
                raise ValueError(f"Duplicate event handler for table {table_id}")
            handlers[table_id] = runtime.workflow2.handle_confirmation
    return handlers


def _make_lifespan(runtime: Runtime, settings: AppSettings, schedules: ScheduleConfig):
    scheduler: BackgroundScheduler | None = None
    stream: BitableEventStream | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal scheduler, stream
        try:
            organization = runtime.organization_cache.refresh()
        except Exception:
            # Use the persisted organization cache if startup refresh fails.
            if runtime.organization_cache.store.load_cache("organization") is None:
                raise
            logger.exception("organization_refresh_failed_using_persisted_cache")
            organization = runtime.organization_cache.get()
        logger.info("organization_cache_loaded persons=%d departments=%d",
                    len(organization.persons), len(organization.departments))
        capture_notification_backlog(runtime)
        if settings.workflow2_enabled and runtime.workflow2 is not None:
            for kind in ("stage", "monthly", "weekly"):
                runtime.workflow2._check_ready(kind)
            activation = runtime.workflow2.store.activate(
                datetime.now(ZoneInfo(schedules.timezone)).date())
            logger.info("workflow2_activated activation=%s", activation)
        if settings.event_stream_enabled:
            stream = BitableEventStream(
                handlers=_event_handlers(runtime),
                bitable=runtime.infrastructure.bitable,
                settings=settings,
            )
            stream.start()
        if settings.scheduler_enabled:
            scheduler = _build_scheduler(schedules, runtime, settings)
            scheduler.start()
        logger.info(
            "service_started scheduler_enabled=%s event_stream_enabled=%s "
            "workflow2_enabled=%s jobs=%d timezone=%s",
            settings.scheduler_enabled, settings.event_stream_enabled,
            settings.workflow2_enabled,
            len(scheduler.get_jobs()) if scheduler else 0, schedules.timezone,
        )
        try:
            yield
        finally:
            logger.info("service_stopping")
            if stream:
                stream.stop()
            if scheduler:
                scheduler.shutdown(wait=False)
            logger.info("service_stopped")

    return lifespan


def _schedule_daily(
    scheduler: BackgroundScheduler,
    schedules: ScheduleConfig,
    schedule: WorkflowSchedule,
    binding: WorkflowBinding,
) -> None:
    if schedule.schedule_type != "daily":
        raise ValueError(f"Registered workflow {binding.name} must have a daily schedule")
    hour, minute = (int(value) for value in schedule.time.split(":", 1))

    def run_daily() -> None:
        days_ago = int(schedule.options.get("material_days_ago", 1))
        today = datetime.now(ZoneInfo(schedules.timezone)).date()
        target = today - timedelta(days=days_ago)
        logger.info("scheduled_workflow1_run date=%s", target)
        binding.workflow.start(target)

    scheduler.add_job(
        run_daily, "cron", hour=hour, minute=minute, id=binding.name,
        replace_existing=True, max_instances=1, coalesce=True,
    )


def _build_scheduler(
    schedules: ScheduleConfig, runtime: Runtime, settings: AppSettings
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=schedules.timezone)
    for name, binding in runtime.workflows.items():
        schedule = schedules.workflows.get(name)
        if schedule is None:
            raise ValueError(f"Missing schedule for registered workflow {name}")
        if schedule.enabled:
            _schedule_daily(scheduler, schedules, schedule, binding)
            if notify_at := schedule.options.get("notify_at"):
                hour, minute = map(int, str(notify_at).split(":"))

                def notify_evaluators(one: WorkflowBinding = binding,
                                      daily: WorkflowSchedule = schedule) -> None:
                    today = datetime.now(ZoneInfo(schedules.timezone)).date()
                    days_ago = int(daily.options.get("material_days_ago", 1))
                    one.workflow.notify_evaluators_if_due(today - timedelta(days=days_ago))

                scheduler.add_job(
                    notify_evaluators, "cron", hour=hour, minute=minute,
                    id=f"{name}_evaluator_notification", replace_existing=True,
                    max_instances=1, coalesce=True,
                )
            if report_notify_at := schedule.options.get("report_notify_at"):
                hour, minute = map(int, str(report_notify_at).split(":"))

                def notify_reports(one: WorkflowBinding = binding,
                                   daily: WorkflowSchedule = schedule) -> None:
                    today = datetime.now(ZoneInfo(schedules.timezone)).date()
                    days_ago = int(daily.options.get("material_days_ago", 1))
                    one.workflow.notify_documents_if_due(today - timedelta(days=days_ago))

                scheduler.add_job(
                    notify_reports, "cron", hour=hour, minute=minute,
                    id=f"{name}_report_notification", replace_existing=True,
                    max_instances=1, coalesce=True,
                )

        if binding.auto_advance_at:
            hour, minute = (int(value) for value in binding.auto_advance_at.split(":", 1))

            def auto_advance(one: WorkflowBinding = binding) -> None:
                for run in one.store.list_incomplete_workflows():
                    result = one.workflow.finalize_pending_confirmations(run.target_date)
                    logger.info("auto_advance_result date=%s result=%s",
                                run.target_date, result)

            scheduler.add_job(
                auto_advance, "cron", hour=hour, minute=minute,
                id=f"{name}_confirmation_auto_advance", replace_existing=True,
                max_instances=1, coalesce=True,
            )

    def cleanup() -> None:
        today = datetime.now(ZoneInfo(schedules.timezone)).date()
        removed = sum(
            binding.store.cleanup_completed(settings.snapshot_retention_days, today=today)
            for binding in runtime.workflows.values()
        )
        logger.info("snapshot_cleanup_removed removed=%d retention_days=%d",
                    removed, settings.snapshot_retention_days)

    scheduler.add_job(
        cleanup, "cron", hour=3, minute=30, id="snapshot_cleanup",
        replace_existing=True, max_instances=1, coalesce=True,
    )

    def refresh_organization() -> None:
        try:
            runtime.organization_cache.refresh()
        except Exception:
            logger.exception("organization_refresh_failed")

    # 组织缓存每日刷新：需早于 workflow1_daily（默认 01:00），保证当天日报
    # 使用最新的人员名单；刷新失败仅通知管理员，不影响后续任务继续执行。
    scheduler.add_job(
        refresh_organization, "cron", hour=0, minute=50, id="organization_refresh",
        replace_existing=True, max_instances=1, coalesce=True,
    )
    skill_review = schedules.workflows.get("skill_prompt_review")
    if (runtime.prompt_repository is not None and skill_review is not None
            and skill_review.enabled):
        def review_skill_prompts() -> None:
            organization = runtime.organization_cache.get()
            runtime.prompt_repository.review_pending(organization)

        # Use the freshly refreshed organization relations and finish before
        # Workflow1 starts at 01:00.
        review_hour, review_minute = map(int, skill_review.time.split(":"))
        scheduler.add_job(
            review_skill_prompts, "cron", hour=review_hour, minute=review_minute,
            id="skill_prompt_review", replace_existing=True,
            max_instances=1, coalesce=True,
        )
    if settings.workflow2_enabled and runtime.workflow2 is not None:
        def run_workflow2(kind: str) -> None:
            today = datetime.now(ZoneInfo(schedules.timezone)).date()
            run = runtime.workflow2.scheduled(kind, today)
            if run is None:
                logger.info("workflow2_not_due kind=%s date=%s", kind, today)

        for kind, schedule_name in (("stage", "s04_progress"),
                                    ("monthly", "s05_people_suggestions"),
                                    ("weekly", "s08_public_technology")):
            item = schedules.workflows[schedule_name]
            if not item.enabled:
                continue
            hour, minute = map(int, item.time.split(":"))
            options = {}
            if item.schedule_type == "weekly":
                options["day_of_week"] = item.options["weekday"]
            elif item.schedule_type == "monthly":
                options["day"] = item.options["day"]
            scheduler.add_job(run_workflow2, "cron", args=[kind], hour=hour,
                minute=minute, **options, id=f"workflow2_{kind}",
                replace_existing=True, max_instances=1, coalesce=True)
            if notify_at := item.options.get("notify_at"):
                notify_hour, notify_minute = map(int, str(notify_at).split(":"))

                def notify_workflow2(cycle_kind: str) -> None:
                    today = datetime.now(ZoneInfo(schedules.timezone)).date()
                    runtime.workflow2.notify_due(cycle_kind, today)

                scheduler.add_job(notify_workflow2, "cron", args=[kind],
                    hour=notify_hour, minute=notify_minute, **options,
                    id=f"workflow2_{kind}_notification", replace_existing=True,
                    max_instances=1, coalesce=True)
        scheduler.add_job(runtime.workflow2.recover, "interval", minutes=15,
            id="workflow2_recovery", replace_existing=True, max_instances=1,
            coalesce=True)
    return scheduler

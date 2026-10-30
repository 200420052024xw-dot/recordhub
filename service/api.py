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
from config.schedules import ScheduleConfig
from schema import Organization, PersonCreateRequest, PersonUpdateRequest
from service.bitable_events import BitableEventStream
from service.runtime import Runtime, build_runtime, capture_notification_backlog
from service.workbuddy_query.models import IdentityRequest, PreviousEvaluationRequest, QueryRequest
from service.workbuddy_review.models import PendingRequest, ReviewSubmitRequest
from workflow1.models import (
    ConfirmationRequest,
    WorkBuddyIdentityRequest,
    WorkBuddySubmitRequest,
)
from workflow1.workbuddy_inbox import LogSubmitService
from tool.diagnostics import error_summary, execution_context, diagnostic_context

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
        from service.alerts import AdminAlertHandler, configure_admin_alerts
        from config.logs import RetainedJSONHandler, setup_logging
        if not any(isinstance(h, RetainedJSONHandler) for h in logging.getLogger().handlers):
            setup_logging(settings)
        if not any(isinstance(h, AdminAlertHandler) for h in logging.getLogger().handlers):
            configure_admin_alerts(settings)
    active_runtime = runtime or build_runtime(settings)
    schedules = load_schedule_config(settings.schedule_config_path)
    app = FastAPI(
        title="RecordHub",
        version="0.2.0",
        lifespan=_make_lifespan(active_runtime, settings, schedules),
    )
    app.state.runtime = active_runtime

    @app.middleware("http")
    async def diagnostic_request(request: Request, call_next):
        from uuid import uuid4
        from time import monotonic
        execution_id = uuid4().hex
        request.state.execution_id = execution_id
        started = monotonic()
        with diagnostic_context(execution_id=execution_id, operation="http", method=request.method,
                                path=request.url.path):
            try:
                response = await call_next(request)
            except Exception:
                logger.info("http_request_finished", extra={"fields": {"status": "failed",
                    "elapsed_ms": round((monotonic() - started) * 1000)}})
                raise
            response.headers["X-RecordHub-Execution-ID"] = execution_id
            logger.info("http_request_finished", extra={"fields": {"status_code": response.status_code,
                "elapsed_ms": round((monotonic() - started) * 1000)}})
            return response

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        with diagnostic_context(execution_id=getattr(request.state, "execution_id", "-"), operation="http",
                                method=request.method, path=request.url.path):
            logger.error("http_request_failed method=%s path=%s error=%s",
                         request.method, request.url.path, error_summary(exc),
                         exc_info=(type(exc), exc, exc.__traceback__))
        return JSONResponse(status_code=500, content={"detail": "Internal server error; see service logs"},
            headers={"X-RecordHub-Execution-ID": getattr(request.state, "execution_id", "-")})

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

    @app.post("/admin/submission-statistics/{target_date}", dependencies=[Depends(require_admin)])
    def refresh_submission_statistics(target_date: date) -> dict:
        return workflow1.workflow.refresh_submission_statistics(target_date)

    @app.post("/admin/reviews/merge/{target_date}", dependencies=[Depends(require_admin)])
    def merge_reviews(target_date: date) -> dict:
        """手动把对话入口收下的批改结果合并进飞书表（正常由 21:40 定时触发）。"""
        if runtime.review is None:
            raise HTTPException(status_code=503, detail="Review endpoint is not configured")
        return runtime.review.merge(target_date)

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

    @app.post("/admin/workflow2/retry", dependencies=[Depends(require_admin)])
    def retry_workflow2(kind: str = "all") -> dict:
        """手动重试卡住的周期任务：S04 阶段分析用 kind=stage，S05—S09 用 monthly/weekly。

        缺省重试全部；无视自动重试预算，只补没完成的部分（已出草稿的部门/技能会跳过）。
        """
        if runtime.workflow2 is None:
            raise HTTPException(status_code=503, detail="Workflow2 is unavailable")
        try:
            runs = runtime.workflow2.retry_blocked(kind)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        logger.info("workflow2_manual_retry_finished kind=%s retried=%d", kind, len(runs))
        return {"retried": [{"run_id": run.run_id, "kind": run.kind,
                             "scheduled_date": run.scheduled_date.isoformat(),
                             "status": run.status, "last_error": run.last_error}
                            for run in runs]}

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

    # 对话入口的查询口径:又一把独立 token,拿不到 /admin,也读不了提交接口。
    def require_query(
        authorization: Annotated[str | None, Header()] = None
    ) -> None:
        _require_token(settings.query_token, authorization)

    def query_service():
        if runtime.query is None:
            raise HTTPException(
                status_code=503, detail="Query endpoint is not configured")
        return runtime.query

    @app.post("/workbuddy/v1/work-logs/previous-evaluation",
              dependencies=[Depends(require_workbuddy)])
    def workbuddy_previous_evaluation(request: PreviousEvaluationRequest) -> dict:
        return query_service().previous_evaluation(request.person_id)

    @app.post("/workbuddy/v1/queries/identity",
              dependencies=[Depends(require_query)])
    def workbuddy_query_identity(request: IdentityRequest) -> dict:
        return query_service().identity(request.name)

    @app.post("/workbuddy/v1/queries",
              dependencies=[Depends(require_query)])
    def workbuddy_query(request: QueryRequest) -> dict:
        return query_service().query(request)

    # 批改口径:又一把独立 token,拿不到 /admin,也读不了查询 / 提交接口。
    def require_review(
        authorization: Annotated[str | None, Header()] = None
    ) -> None:
        _require_token(settings.review_token, authorization)

    def review_service():
        if runtime.review is None:
            raise HTTPException(
                status_code=503, detail="Review endpoint is not configured")
        return runtime.review

    @app.post("/workbuddy/v1/reviews/pending",
              dependencies=[Depends(require_review)])
    def workbuddy_reviews_pending(request: PendingRequest) -> dict:
        return review_service().pending(request)

    @app.post("/workbuddy/v1/reviews",
              dependencies=[Depends(require_review)])
    def workbuddy_review_submit(request: ReviewSubmitRequest) -> dict:
        return review_service().submit(request)


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
            if runtime.organization_cache.store.load_cache(
                    runtime.organization_cache.CACHE_KEY) is None:
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
            runtime.orchestrator.restore()
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
                scheduler.pause()
                runtime.orchestrator.tasks.close()
                scheduler.shutdown(wait=True)
            logger.info("service_stopped")

    @asynccontextmanager
    async def observed_lifespan(app: FastAPI):
        with execution_context(operation="service_lifecycle"):
            try:
                async with lifespan(app):
                    yield
            except Exception:
                logger.critical("service_start_failed", exc_info=True)
                raise
            finally:
                from service.alerts import close_admin_alerts
                close_admin_alerts()

    return observed_lifespan


def _build_scheduler(
    schedules: ScheduleConfig, runtime: Runtime, settings: AppSettings
) -> BackgroundScheduler:
    from service.orchestration import Orchestrator

    scheduler = BackgroundScheduler(timezone=schedules.timezone)
    for name in runtime.workflows:
        if name not in schedules.workflows:
            raise ValueError(f"Missing schedule for registered workflow {name}")
    orchestration = Orchestrator(runtime, schedules, scheduler, settings)
    runtime.orchestrator = orchestration

    scheduler.add_job(orchestration.plan_day, "cron", hour=0, minute=0,
                      id="appointment_planner", replace_existing=True, coalesce=True,
                      max_instances=1, misfire_grace_time=None)
    # Repair derived timers only; never restart business workflows by scanning.
    scheduler.add_job(orchestration.tasks.restore, "interval", hours=1,
                      id="appointment_repair", replace_existing=True, coalesce=True,
                      max_instances=1)

    def cleanup() -> None:
        removed = sum(binding.store.cleanup_completed(settings.snapshot_retention_days)
                      for binding in runtime.workflows.values())
        if runtime.workflow2 is not None:
            removed += runtime.workflow2.store.cleanup_completed(settings.snapshot_retention_days)
        removed += orchestration.tasks.journal.cleanup(2)
        logger.info("state_cleanup_removed removed=%d", removed)
        # cleanup_completed never touches an unfinished day, so without this a
        # day that failed once keeps its state file forever and nobody is told.
        # Purge the oldest first: whatever survives is exactly what still
        # deserves a warning, and each stale day yields one message either way.
        for binding in runtime.workflows.values():
            for purged in binding.store.purge_stale_incomplete(
                    settings.stale_incomplete_purge_days):
                linked = purged["external_records"] or purged["notifications"]
                logger.log(
                    logging.ERROR if linked else logging.INFO,
                    "stale_incomplete_purged workflow=%s date=%s status=%s "
                    "external_records=%d notifications=%d last_error=%s",
                    binding.name, purged["target_date"], purged["status"],
                    purged["external_records"], purged["notifications"],
                    purged["last_error"] or "-")
            for run in binding.store.list_stale_incomplete(
                    settings.stale_incomplete_alert_days):
                logger.error(
                    "stale_incomplete_workflow workflow=%s date=%s status=%s last_error=%s",
                    binding.name, run.target_date, run.status, run.last_error or "-")

    scheduler.add_job(cleanup, "cron", hour=3, minute=30, id="snapshot_cleanup",
                      replace_existing=True, max_instances=1, coalesce=True)

    def refresh_organization() -> None:
        try:
            runtime.organization_cache.refresh()
        except Exception:
            logger.exception("organization_refresh_failed")

    scheduler.add_job(refresh_organization, "cron", hour=0, minute=50,
                      id="organization_refresh", replace_existing=True,
                      max_instances=1, coalesce=True)
    return scheduler

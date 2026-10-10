from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from collections.abc import Callable
from typing import Any

from config import AppSettings, load_table_config, load_schedule_config
from data import FileStateStore
from data.repositories import LogRepository, OrganizationCache, OrganizationRepository
from llm import PromptRepository, PromptService
from llm.client import DeepSeekClient
from tool.cloud_docs import CloudDocsService
from tool.feishu import BitableService, ContactService, FeishuClient, MessageService
from tool.http import HttpTransport, UrllibTransport
from tool.notifications import NotificationPolicy, capture_notifications
from workflow1.documents import DailyDocuments
from workflow1.evaluations import AiEvaluationRepository, HumanEvaluationRepository
from workflow1.models import DailySnapshot
from workflow1.reports import WorkflowReportRepository
from workflow1.settings import LogSubmitSettings, W1Settings
from workflow1.workbuddy_inbox import LogSubmitService, WorkBuddyInbox
from workflow1.workflow import Workflow1


@dataclass(frozen=True, slots=True)
class Infrastructure:
    bitable: BitableService
    contacts: ContactService
    messages: MessageService
    llm: DeepSeekClient
    cloud_docs: CloudDocsService


def _build_infrastructure(
    settings: AppSettings, transport: HttpTransport | None = None
) -> Infrastructure:
    http = transport or UrllibTransport(settings.http_timeout_seconds)
    feishu = FeishuClient(settings.feishu, http)
    messages = MessageService(
        feishu, recipient_override=(settings.message_override_open_id
                                    if settings.simulation_mode else "")
    )
    return Infrastructure(
        bitable=BitableService(feishu, settings.feishu.bitable_app_token),
        contacts=ContactService(feishu),
        messages=messages,
        llm=DeepSeekClient(settings.deepseek, http),
        cloud_docs=CloudDocsService(feishu),
    )


def make_recipient_resolver(
    organization_cache: OrganizationCache,
) -> Callable[[str], tuple[str, str] | None]:
    def resolve(open_id: str) -> tuple[str, str] | None:
        organization = organization_cache.get()
        for person in organization.persons:
            if person.open_id == open_id:
                return person.name, person.mobile or ""
        return None

    return resolve


@dataclass(slots=True)
class Runtime:
    settings: AppSettings
    infrastructure: Infrastructure
    organization_cache: OrganizationCache
    workflows: dict[str, WorkflowBinding]
    workflow2: Workflow2 | None = None
    prompt_repository: PromptRepository | None = None
    workbuddy: LogSubmitService | None = None


@dataclass(slots=True)
class WorkflowBinding:
    name: str
    workflow: Any
    store: FileStateStore
    auto_advance_at: str
    confirmation_webhook_token: str
    record_handlers: dict[str, Callable[[str], Any]]


def build_runtime(settings: AppSettings) -> Runtime:
    settings.validate()
    schedules = load_schedule_config(settings.schedule_config_path)
    w1 = W1Settings.from_schedule(schedules)
    tables = load_table_config(settings.table_config_path)
    infrastructure = _build_infrastructure(settings)
    notifications = NotificationPolicy(settings.state_dir)
    store = FileStateStore(
        settings.state_dir,
        workflow_type="workflow1",
        snapshot_model=DailySnapshot,
    )
    organization_cache = OrganizationCache(
        store, OrganizationRepository(
            infrastructure.bitable, infrastructure.contacts, tables,
            use_person_field_ids=(settings.simulation_mode and
                                  bool(settings.message_override_open_id)),
        )
    )
    infrastructure.messages.recipient_resolver = make_recipient_resolver(
        organization_cache
    )
    prompt_service = PromptService(
        infrastructure.llm, max_attempts=settings.llm_max_attempts
    )
    prompt_repository = PromptRepository(
        bitable=infrastructure.bitable, tables=tables, store=store,
        prompt_service=prompt_service, messages=infrastructure.messages,
    )
    ai_evaluations = AiEvaluationRepository(
        infrastructure.bitable, tables, store
    )
    human_evaluations = HumanEvaluationRepository(
        infrastructure.bitable, tables, cutoff_at=w1.auto_advance_at)
    log_repository = LogRepository(infrastructure.bitable, tables, organization_cache)
    workbuddy = LogSubmitService(
        inbox=WorkBuddyInbox(settings.state_dir),
        organization_cache=organization_cache,
        bitable=infrastructure.bitable,
        table_config=tables,
        workflow_store=store,
        settings=LogSubmitSettings.from_schedule(schedules),
    )
    workflow = Workflow1(
        store=store,
        organization_cache=organization_cache,
        log_repository=log_repository,
        ai_evaluations=ai_evaluations,
        human_evaluations=human_evaluations,
        prompt_service=prompt_service,
        prompt_repository=prompt_repository,
        workbuddy=workbuddy,
        messages=infrastructure.messages,
        documents=DailyDocuments(infrastructure.cloud_docs, store,
                                 settings.archive_parent_folder_token),
        reports=WorkflowReportRepository(infrastructure.bitable, tables, store),
        llm_concurrency=settings.llm_concurrency,
        auto_advance_at=w1.auto_advance_at,
        notify_at=w1.notify_at,
        report_notify_at=w1.report_notify_at,
        minister_review_form_url=w1.minister_review_form_url,
        backbone_review_form_url=w1.backbone_review_form_url,
        admin_open_id=settings.admin_open_id,
        notifications=notifications,
    )
    binding = WorkflowBinding(
        name="workflow1_daily",
        workflow=workflow,
        store=store,
        auto_advance_at=w1.auto_advance_at,
        confirmation_webhook_token=w1.confirmation_webhook_token,
        record_handlers={
            table.table_id: partial(workflow.handle_human_record, table_id=table.table_id)
            for table in human_evaluations.tables.values()
        },
    )
    workflow2 = None
    if settings.workflow2_enabled:
        from workflow2.workflow import Workflow2
        from workflow2.feishu_material import SnapshotRebuilder
        from workflow2.materials import MaterialPreparer
        from workflow2.store import Workflow2Store
        from workflow2.tables import Workflow2Tables

        preparer = MaterialPreparer(store, rebuilder=SnapshotRebuilder(
            infrastructure.bitable, tables, organization_cache, log_repository,
            auto_advance_at=w1.auto_advance_at,
        ))
        workflow2 = Workflow2(
            store=Workflow2Store(settings.state_dir), preparer=preparer,
            organization=organization_cache, prompts=prompt_repository,
            tables=Workflow2Tables(infrastructure.bitable, tables),
            prompt_service=prompt_service, messages=infrastructure.messages,
            documents=infrastructure.cloud_docs,
            schedules=schedules,
            archive_parent=settings.archive_parent_folder_token,
            notifications=notifications,
        )
    return Runtime(
        settings=settings,
        infrastructure=infrastructure,
        organization_cache=organization_cache,
        workflows={binding.name: binding},
        workflow2=workflow2,
        prompt_repository=prompt_repository,
        workbuddy=workbuddy,
    )


def capture_notification_backlog(runtime: Runtime) -> None:
    """Retain ready historical messages before snapshot cleanup, with zero sends."""
    import logging
    from datetime import date
    logger = logging.getLogger(__name__)
    daily = runtime.workflows.get("workflow1_daily")
    if daily is None or not isinstance(getattr(daily.workflow, "notifications", None), NotificationPolicy):
        return
    with capture_notifications():
        for path in sorted(daily.store.runs_dir.glob("*.json")):
            target = date.fromisoformat(path.stem)
            try:
                daily.workflow.send_notifications(target)
            except LookupError:
                logger.info("notification_capture_not_ready workflow=workflow1 date=%s", target)
            except Exception:
                logger.exception("notification_capture_failed workflow=workflow1 date=%s", target)
        if runtime.workflow2 is not None:
            for target in sorted({run.scheduled_date for run in runtime.workflow2.store.all_runs()}):
                try:
                    runtime.workflow2.send_notifications(target)
                except Exception:
                    logger.exception("notification_capture_failed workflow=workflow2 date=%s", target)

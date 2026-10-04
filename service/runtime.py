from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from config import AppSettings, load_table_config
from data import FileStateStore
from data.repositories import LogRepository, OrganizationCache, OrganizationRepository
from llm import PromptService
from llm.client import DeepSeekClient
from tool.cloud_docs import CloudDocsService
from tool.feishu import BitableService, ContactService, FeishuClient, MessageService
from tool.http import HttpTransport, UrllibTransport
from workflow1.documents import DailyDocuments
from workflow1.evaluations import AiEvaluationRepository, HumanEvaluationRepository
from workflow1.models import DailySnapshot
from workflow1.reports import WorkflowReportRepository
from workflow1.settings import W1Settings
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
        feishu, recipient_override=settings.message_override_open_id
    )
    return Infrastructure(
        bitable=BitableService(feishu, settings.feishu.bitable_app_token),
        contacts=ContactService(feishu),
        messages=messages,
        llm=DeepSeekClient(settings.deepseek, http),
        cloud_docs=CloudDocsService(feishu),
    )


@dataclass(slots=True)
class Runtime:
    settings: AppSettings
    infrastructure: Infrastructure
    organization_cache: OrganizationCache
    workflows: dict[str, WorkflowBinding]


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
    w1 = W1Settings.from_env()
    tables = load_table_config(settings.table_config_path)
    infrastructure = _build_infrastructure(settings)
    store = FileStateStore(
        settings.state_dir,
        workflow_type="workflow1",
        snapshot_model=DailySnapshot,
    )
    organization_cache = OrganizationCache(
        store, OrganizationRepository(
            infrastructure.bitable, infrastructure.contacts, tables,
            use_person_field_ids=bool(settings.message_override_open_id),
        )
    )
    ai_evaluations = AiEvaluationRepository(
        infrastructure.bitable, tables, store
    )
    human_evaluations = HumanEvaluationRepository(infrastructure.bitable, tables)
    workflow = Workflow1(
        store=store,
        organization_cache=organization_cache,
        log_repository=LogRepository(
            infrastructure.bitable, tables, organization_cache
        ),
        ai_evaluations=ai_evaluations,
        human_evaluations=human_evaluations,
        prompt_service=PromptService(
            infrastructure.llm, max_attempts=settings.llm_max_attempts
        ),
        messages=infrastructure.messages,
        documents=DailyDocuments(infrastructure.cloud_docs, store,
                                 settings.archive_parent_folder_token),
        reports=WorkflowReportRepository(infrastructure.bitable, tables, store),
        llm_concurrency=settings.llm_concurrency,
        auto_advance_at=w1.auto_advance_at,
        admin_open_id=settings.admin_open_id,
    )
    binding = WorkflowBinding(
        name="workflow1_daily",
        workflow=workflow,
        store=store,
        auto_advance_at=w1.auto_advance_at,
        confirmation_webhook_token=w1.confirmation_webhook_token,
        record_handlers={
            human_evaluations.table.table_id: workflow.handle_human_record,
        },
    )
    return Runtime(
        settings=settings,
        infrastructure=infrastructure,
        organization_cache=organization_cache,
        workflows={binding.name: binding},
    )

from __future__ import annotations

from dataclasses import dataclass

from config import load_table_config
from llm import PromptService
from data.repositories import (
    LogRepository,
    OrganizationCache,
    OrganizationRepository,
)
from data import FileStateStore
from workflow1.workflow import Workflow1
from workflow1.documents import DailyDocuments
from data.workflow1_evaluations import AiEvaluationRepository, HumanEvaluationRepository
from data.workflow1_reports import WorkflowReportRepository
from config import AppSettings
from tool.infrastructure import Infrastructure, build_infrastructure


@dataclass(slots=True)
class Runtime:
    settings: AppSettings
    infrastructure: Infrastructure
    store: FileStateStore
    organization_cache: OrganizationCache
    workflow: Workflow1
    human_evaluations: HumanEvaluationRepository


def build_runtime(settings: AppSettings) -> Runtime:
    settings.validate()
    tables = load_table_config(settings.table_config_path)
    infrastructure = build_infrastructure(settings)
    store = FileStateStore(settings.state_dir)
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
        notifications=infrastructure.notifications,
        documents=DailyDocuments(infrastructure.cloud_docs, store,
                                 settings.archive_parent_folder_token),
        reports=WorkflowReportRepository(infrastructure.bitable, tables, store),
        llm_concurrency=settings.llm_concurrency,
        auto_advance_at=settings.auto_advance_at,
    )
    return Runtime(
        settings=settings,
        infrastructure=infrastructure,
        store=store,
        organization_cache=organization_cache,
        workflow=workflow,
        human_evaluations=human_evaluations,
    )

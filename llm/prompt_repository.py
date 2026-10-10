"""Reviewed, per-user prompt templates backed by the Feishu Skill table."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel

from data.store import FileStateStore
from llm.errors import LLMError
from llm.service import PromptService
from schema import Organization, TableConfig
from tool.bitable_fields import record_fields, references, scalar
from tool.diagnostics import error_summary
from tool.feishu import BitableService, MessageService

logger = logging.getLogger(__name__)


FUNCTION_CODES = {
    "日志评价": "S01",
    "阶段工作分析": "S04",
    "降级—奖励—退出—提拔": "S05",
    "idea 清单": "S06",
    "学生例会重点人员": "S07",
    "公共技术": "S08",
    "公共培训建议": "S09",
}


class _AuditModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _S01Output(_AuditModel):
    positive: str
    improvement: str


class _S04Item(_AuditModel):
    work_item: str
    participant_ids: list[str]
    current_progress: str
    main_difficulties: str


class _S05Item(_AuditModel):
    action: str
    person_id: str
    reason: str
    facts: list[str]


class _S06Item(_AuditModel):
    idea: str
    proposer_id: str


class _S07Item(_AuditModel):
    person_id: str
    categories: list[str]
    evidence: str


class _S08Item(_AuditModel):
    technology_name: str
    category: str
    problem_solved: str
    department_ids: list[str]
    contributor_ids: list[str]
    existing_achievements: str
    achievement_refs: list[str]
    suitable_scenarios: str
    repository_suggestion: str
    pending_items: list[str]


class _S09Item(_AuditModel):
    topic: str
    target_audience: str
    target_person_ids: list[str]
    common_need: str
    evidence: str
    expected_effect: str
    available_resource_ids: list[str]
    approach: str
    course_suggestion: str


def _items_model(name: str, item_model: type[_AuditModel]) -> type[_AuditModel]:
    return type(name, (_AuditModel,), {
        "__annotations__": {"items": list[item_model]},
        "items": Field(min_length=1),
    })


AUDIT_OUTPUTS: dict[str, type[BaseModel]] = {
    "S01": _S01Output,
    "S04": _items_model("_S04Output", _S04Item),
    "S05": _items_model("_S05Output", _S05Item),
    "S06": _items_model("_S06Output", _S06Item),
    "S07": _items_model("_S07Output", _S07Item),
    "S08": _items_model("_S08Output", _S08Item),
    "S09": _items_model("_S09Output", _S09Item),
}


class _AuditInput(RootModel[dict[str, Any]]):
    pass


def _audit_input(code: str) -> _AuditInput:
    if code == "S01":
        return _AuditInput({
            "target_date": "2026-01-01",
            "person_id": "TEST-STUDENT",
            "log_id": "TEST-LOG",
            "log": "工作进展：完成测试模块并记录结果。工作困难：接口偶尔超时。心得反思：需要增加重试。其他：无。",
        })
    return _AuditInput({
        "run_id": "skill-format-review",
        "scope": {
            "start_date": "2026-01-01",
            "end_date": "2026-01-01",
            "department_ids": ["TEST-DEPARTMENT"],
            "complete": True,
            "missing_sources": [],
            "omitted_sources": [],
        },
        "records": [{
            "record_id": "TEST-RECORD",
            "source_version": "1",
            "person_id": "TEST-STUDENT",
            "name": "测试学生",
            "department_id": "TEST-DEPARTMENT",
            "department_name": "测试部门",
            "role": "基层学生",
            "work_start": "2026-01-01",
            "work_end": "2026-01-01",
            "submitted": True,
            "confirmed": True,
            "progress": "完成一个可复用的日志检查工具并验证有效。",
            "difficulties": "团队多人遇到相同的接口超时问题。",
            "reflection": "建议整理公共技术文档并组织一次培训。",
            "other": "提出自动汇总日志的 idea，希望在例会上继续讨论。",
            "full_log": "",
            "achievement_refs": ["TEST-ACHIEVEMENT"],
            "evaluations": [],
        }],
        "resources": [{
            "resource_id": "TEST-RESOURCE",
            "name": "测试课程",
            "category": "课程",
            "description": "接口稳定性基础课程",
            "source_record_ids": ["TEST-RECORD"],
        }],
    })


AUDIT_SUFFIX = """

这是系统的输出格式审核。请基于测试材料输出一个合法 JSON 对象。
为验证嵌套结构，S04—S09 的 items 必须且只能包含一个测试条目；不要输出 Markdown 或额外说明。
本次只测试字段、层级和数据类型，内容本身不会用于真实业务。
""".strip()


class PromptEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str
    skill_code: str
    function: str
    template: str
    record_id: str
    created_time: int = 0
    reviewed_at: datetime


class PromptRepository:
    """Audits blank Skill rows and exposes the one active prompt per user/function."""

    CACHE_KEY = "skill_prompt_cache"
    REQUIRED_FIELDS = {
        "user_ref", "role", "template", "function",
        "review_result", "failure_reason",
    }

    def __init__(self, *, bitable: BitableService, tables: TableConfig,
                 store: FileStateStore, prompt_service: PromptService,
                 messages: MessageService) -> None:
        self.bitable = bitable
        self.store = store
        self.prompt_service = prompt_service
        self.messages = messages
        self.lock = threading.RLock()
        table = tables.tables.get("prompts")
        if table is None or not table.table_id.strip():
            raise ValueError("缺少 Skill 配置表")
        missing = sorted(self.REQUIRED_FIELDS - set(table.fields))
        if missing:
            raise ValueError(f"Skill 配置表缺少字段映射：{', '.join(missing)}")
        self.table = table
        self.entries: dict[str, PromptEntry] = {}
        cached = self.store.load_cache(self.CACHE_KEY)
        if isinstance(cached, dict):
            for key, value in cached.get("entries", {}).items():
                try:
                    self.entries[str(key)] = PromptEntry.model_validate(value)
                except ValueError:
                    logger.warning("skill_prompt_cache_entry_invalid key=%s", key,
                                   exc_info=True)

    @staticmethod
    def _key(user_id: str, skill_code: str) -> str:
        return f"{user_id}:{skill_code}"

    def resolve(self, skill_code: str, user_id: str) -> PromptEntry | None:
        with self.lock:
            entry = self.entries.get(self._key(user_id.strip(), skill_code.strip()))
            return entry.model_copy(deep=True) if entry else None

    def _save(self) -> None:
        self.store.save_cache(self.CACHE_KEY, {
            "entries": {key: value.model_dump(mode="json")
                        for key, value in self.entries.items()}
        })

    @staticmethod
    def _people_aliases(organization: Organization) -> dict[str, str | None]:
        aliases: dict[str, str | None] = {}
        for person in organization.persons:
            for value in (person.person_id, person.source_record_id,
                          person.open_id, person.name):
                text = (value or "").strip()
                if not text:
                    continue
                if text in aliases and aliases[text] != person.person_id:
                    aliases[text] = None
                else:
                    aliases[text] = person.person_id
        return aliases

    def _resolve_user_id(self, record: dict[str, Any],
                         organization: Organization) -> str:
        fields = record_fields(record)
        f = self.table.fields
        aliases = self._people_aliases(organization)
        matched = {aliases.get(item) for item in references(fields.get(f["user_ref"]))}
        matched.discard(None)
        if len(matched) != 1:
            raise ValueError("使用人必须唯一关联到人员表中的有效人员")
        user_id = next(iter(matched))
        person = organization.person_map().get(user_id)
        if person is None or not person.active:
            raise ValueError("使用人不在当前有效人员名单中")
        return user_id

    def _parse(self, record: dict[str, Any], organization: Organization) -> PromptEntry:
        record_id = str(record.get("record_id") or "").strip()
        if not record_id:
            raise ValueError("记录缺少 record_id")
        fields = record_fields(record)
        f = self.table.fields
        user_id = self._resolve_user_id(record, organization)
        person = organization.person_map()[user_id]
        role = scalar(fields.get(f["role"])).strip()
        if role != person.role:
            raise ValueError(f"角色查找值“{role or '空'}”与人员表角色“{person.role}”不一致")
        function = scalar(fields.get(f["function"])).strip()
        skill_code = FUNCTION_CODES.get(function)
        if skill_code is None:
            raise ValueError(f"未知功能：{function or '空'}")
        template = scalar(fields.get(f["template"])).strip()
        if not template:
            raise ValueError("Skill内容不能为空")
        return PromptEntry(
            user_id=user_id, skill_code=skill_code, function=function,
            template=template, record_id=record_id,
            created_time=int(record.get("created_time") or 0),
            reviewed_at=datetime.now(timezone.utc),
        )

    def _update_review(self, record_id: str, result: str, reason: str = "") -> None:
        f = self.table.fields
        self.bitable.update_record(self.table.table_id, record_id, {
            f["review_result"]: result,
            f["failure_reason"]: reason,
        })

    def _notify_failure(self, *, user_id: str | None, function: str,
                        record_id: str, reason: str,
                        organization: Organization) -> None:
        person = organization.person_map().get(user_id or "")
        if person is None or not person.open_id:
            logger.warning("skill_review_failure_missing_open_id record_id=%s user_id=%s",
                           record_id, user_id or "-")
            return
        self.messages.send_text(
            person.open_id,
            f"你的“{function or '未知功能'}”Skill 格式审核未通过。\n"
            f"原因：{reason}\n请新增一条修正后的 Skill 记录，系统将在下一次审核时检测。",
            idempotency_key=f"skill-review:{record_id}:failed",
        )

    def _audit(self, entry: PromptEntry) -> None:
        self.prompt_service.execute(
            prompt_code=f"{entry.skill_code}-FORMAT-REVIEW",
            template=f"{entry.template}\n\n{AUDIT_SUFFIX}",
            input_data=_audit_input(entry.skill_code),
            output_model=AUDIT_OUTPUTS[entry.skill_code],
        )

    def review_pending(self, organization: Organization) -> dict[str, int]:
        """Review blank rows, normalize active versions, and persist the cache."""
        with self.lock:
            records = self.bitable.list_records(self.table.table_id)
            f = self.table.fields
            parsed: dict[str, PromptEntry] = {}
            statuses: dict[str, str] = {}
            result = {"reviewed": 0, "passed": 0, "failed": 0}
            for record in records:
                record_id = str(record.get("record_id") or "")
                statuses[record_id] = scalar(
                    record_fields(record).get(f["review_result"])).strip()
                try:
                    parsed[record_id] = self._parse(record, organization)
                except ValueError as exc:
                    if not statuses[record_id]:
                        reason = str(exc)
                        result["reviewed"] += 1
                        result["failed"] += 1
                        self._update_review(record_id, "未通过", reason)
                        try:
                            user_id = self._resolve_user_id(record, organization)
                        except ValueError:
                            user_id = None
                        try:
                            self._notify_failure(
                                user_id=user_id,
                                function=scalar(record_fields(record).get(f["function"])).strip(),
                                record_id=record_id, reason=reason,
                                organization=organization)
                        except Exception:
                            logger.exception(
                                "skill_review_failure_notification_failed record_id=%s",
                                record_id)
                        logger.warning("skill_review_rejected record_id=%s reason=%s",
                                       record_id, reason)
                    else:
                        logger.warning("skill_record_invalid record_id=%s status=%s reason=%s",
                                       record_id, statuses[record_id], exc)

            # Rebuild from table truth so a deleted local cache recovers automatically.
            approved: dict[str, PromptEntry] = {}
            for record_id, entry in parsed.items():
                if statuses.get(record_id) != "通过":
                    continue
                key = self._key(entry.user_id, entry.skill_code)
                previous = approved.get(key)
                if previous is None or (entry.created_time, entry.record_id) > (
                        previous.created_time, previous.record_id):
                    approved[key] = entry
            for record_id, entry in parsed.items():
                if statuses.get(record_id) != "通过":
                    continue
                active = approved[self._key(entry.user_id, entry.skill_code)]
                if active.record_id != record_id:
                    self._update_review(record_id, "未使用", "")
            self.entries = approved

            pending = sorted(
                (entry for record_id, entry in parsed.items()
                 if not statuses.get(record_id)),
                key=lambda item: (item.created_time, item.record_id),
            )
            for entry in pending:
                result["reviewed"] += 1
                try:
                    self._audit(entry)
                except LLMError as exc:
                    reason = ("固定案例输出无法按规定格式解析：" +
                              error_summary(exc))[:1500]
                    try:
                        self._update_review(entry.record_id, "未通过", reason)
                    finally:
                        try:
                            self._notify_failure(
                                user_id=entry.user_id, function=entry.function,
                                record_id=entry.record_id, reason=reason,
                                organization=organization)
                        except Exception:
                            logger.exception("skill_review_failure_notification_failed record_id=%s",
                                             entry.record_id)
                    result["failed"] += 1
                    logger.warning("skill_review_failed record_id=%s reason=%s",
                                   entry.record_id, reason, exc_info=True)
                    continue
                key = self._key(entry.user_id, entry.skill_code)
                old = self.entries.get(key)
                self._update_review(entry.record_id, "通过", "")
                if old and old.record_id != entry.record_id:
                    self._update_review(old.record_id, "未使用", "")
                self.entries[key] = entry
                result["passed"] += 1
                logger.info("skill_review_passed record_id=%s user_id=%s code=%s",
                            entry.record_id, entry.user_id, entry.skill_code)
            self._save()
            logger.info("skill_review_completed records=%d reviewed=%d passed=%d failed=%d",
                        len(records), result["reviewed"], result["passed"], result["failed"])
            return result

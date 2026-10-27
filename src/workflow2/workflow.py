"""Department-first periodic analysis, form confirmation, and team reports."""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from workflow2.materials import MaterialPreparer
from workflow2.periods import due, material_window
from config.schedules import ScheduleConfig
from data.repositories import OrganizationCache
from data.store import utc_now
from llm import PromptRepository, PromptService
from schema import Organization
from workflow2.skills import SkillRunner, TechnologyOutput, TrainingOutput
from tool.bitable_fields import field_datetime, record_fields, references, scalar
from tool.cloud_docs import (CloudDocsService, divider_block, label_value_block,
                             markdown_blocks, paragraph_block, text_block)
from tool.feishu import MessageService
from tool.diagnostics import error_summary, workflow_diagnostics, diagnostic_context, stage_diagnostics
from tool.notifications import NotificationPolicy, manual_notifications, record_outcome
from workflow1.documents import DailyDocuments
from workflow2.models import (CycleRun, DepartmentAnalysis, DepartmentResult,
                              WeeklySummaryInput)
from workflow2.store import Workflow2Store
from workflow2.tables import Workflow2Tables, date_millis


SCHEDULES = {"stage": "s04_progress", "monthly": "s05_people_suggestions",
             "weekly": "s08_public_technology"}
CODES = {"stage": ("S04",), "monthly": ("S05", "S06", "S07"),
         "weekly": ("S08", "S09")}
NAMES = {"S04": "阶段工作进展", "S05": "人员建议", "S06": "idea清单",
         "S07": "重点谈话", "S08": "公共技术", "S09": "公共培训"}
RESULT_COLUMNS = {"S05": "personnel_s05", "S06": "ideas_s06", "S07": "meeting_s07"}
SUMMARY_MODELS = {"S08": TechnologyOutput, "S09": TrainingOutput}
CONFIRM_COLUMNS = {"S05": ("personnel_decision", "personnel_edited"),
                   "S06": ("ideas_decision", "ideas_edited"),
                   "S07": ("meeting_decision", "meeting_edited")}
SHANGHAI = ZoneInfo("Asia/Shanghai")

logger = logging.getLogger(__name__)


def _write_date_millis() -> int:
    """Date shown in Feishu is the local day the record is written."""
    return date_millis(utc_now().astimezone(SHANGHAI).date())


def _partial_notice(omitted: set[str]) -> str:
    return (f"【材料说明】本周期有 {len(omitted)} 项材料缺失或尚未完成；"
            "以下仅分析可用记录。\n") if omitted else ""


FIELD_LABELS = {
    "work_item": "工作事项",
    "current_progress": "当前进展",
    "main_difficulties": "主要困难",
    "name": "姓名",
    "result": "建议类型",
    "reason": "理由",
    "idea": "想法",
    "focus": "关注类别",
    "type": "类别",
    "problem": "解决的问题",
    "contributors": "贡献者",
    "achievements": "已有成果",
    "usage": "适用场景",
    "suggestion": "建议",
    "todo": "待完善事项",
    "topic": "培训主题",
    "audience": "面向人员",
    "needs": "共同需求",
    "goals": "预期效果",
    "resources": "可用资源",
    "method": "开展方式",
}

# 已并进“涉及人员”展示的姓名列表字段，不再重复出现在“具体内容”里。
_NAME_FIELDS = {"participant_names", "proposers", "contributors"}


def _subject(item: dict, code: str) -> str:
    if code == "S05":
        return f"{item.get('name', '')}｜{item.get('result', '')}"
    if code == "S06":
        idea = str(item.get("idea", "")).strip().split("\n")[0].strip()
        return idea[:40] or "idea"
    for key in ("work_item", "topic", "name"):
        if item.get(key):
            return str(item[key])
    return NAMES[code]


def _subject_fields(code: str) -> set[str]:
    if code == "S05":
        return {"name", "result"}
    if code == "S09":
        return {"topic"}
    if code in ("S08", "S10"):
        return {"name"}
    if code == "S04":
        return {"work_item"}
    return set()


def _names_of(item: dict) -> list[str]:
    names: list[str] = []
    for key in ("participant_names", "proposers", "contributors"):
        for name in item.get(key) or []:
            if name not in names:
                names.append(name)
    return names


def _item_plain(item: dict, code: str) -> str:
    """多维表格里的一项纯文本；姓名已直接给出，无需解析编号。"""
    lines = [f"-结果类别：{NAMES[code]}", f"-主题或对象：{_subject(item, code)}"]
    skip = set(_NAME_FIELDS) | _subject_fields(code)
    for key, value in item.items():
        if key in skip or value in ("", [], None):
            continue
        rendered = "、".join(map(str, value)) if isinstance(value, list) else str(value)
        lines.append(f"-{FIELD_LABELS.get(key, key)}：{rendered}")
    names = _names_of(item)
    if names:
        lines.append(f"-涉及人员：{'、'.join(names)}")
    return "\n".join(lines)


def _result_text(result: dict, code: str, organization: Organization | None = None,
                 department_ids: list[str] | None = None) -> str:
    content = result.get("content") or {}
    items = content.get("items") or []
    return "\n\n".join(_item_plain(item, code) for item in items) \
        or "本范围内暂无可确认的分析条目。"


def _doc_field(blocks: list[dict], label: str, value):
    """文档里渲染一个字段：短值/列表并成一行，长文字保留 Markdown 块。"""
    if value in ("", [], None):
        return
    if isinstance(value, list):
        blocks.append(label_value_block(f"{label}：", "、".join(str(entry) for entry in value)))
        return
    text = str(value)
    if "\n" not in text and not text.startswith("- ") and "**" not in text:
        blocks.append(label_value_block(f"{label}：", text))
    else:
        blocks.append(paragraph_block(label, bold=True))
        blocks.extend(markdown_blocks(text))


def _result_doc_blocks(result: dict, code: str) -> list[dict]:
    """把 Skill 结果渲染成飞书文档块（标题 + 加粗标签 + Markdown 项目符号）。"""
    content = result.get("content") or {}
    items = content.get("items") or []
    blocks: list[dict] = []
    for index, item in enumerate(items, 1):
        if code == "S04":
            blocks.append(text_block(str(item.get("work_item", "工作事项")), heading=2))
            names = "、".join(item.get("participant_names") or [])
            if names:
                blocks.append(label_value_block("参与人员：", names))
            _doc_field(blocks, "当前进展", item.get("current_progress"))
            _doc_field(blocks, "主要困难", item.get("main_difficulties"))
        elif code == "S05":
            blocks.append(text_block(f"{item.get('name', '')}｜{item.get('result', '')}",
                                    heading=2))
            _doc_field(blocks, "理由", item.get("reason"))
        elif code == "S06":
            blocks.append(text_block(f"idea {index}", heading=2))
            names = "、".join(item.get("proposers") or [])
            if names:
                blocks.append(label_value_block("提出者：", names))
            _doc_field(blocks, "内容", item.get("idea"))
        elif code == "S07":
            blocks.append(text_block(str(item.get("name", "")), heading=2))
            focus = "、".join(item.get("focus") or [])
            if focus:
                blocks.append(label_value_block("关注类别：", focus))
            _doc_field(blocks, "依据", item.get("reason"))
        elif code in ("S08", "S10"):
            blocks.append(text_block(str(item.get("name", "")), heading=2))
            for key, label in (("type", "类别"), ("problem", "解决的问题"),
                               ("contributors", "贡献者"), ("achievements", "已有成果"),
                               ("usage", "适用场景"), ("suggestion", "入库建议"),
                               ("todo", "待完善")):
                _doc_field(blocks, label, item.get(key))
        elif code == "S09":
            blocks.append(text_block(str(item.get("topic", "")), heading=2))
            for key, label in (("audience", "面向人员"), ("needs", "共同需求"),
                               ("reason", "依据"), ("goals", "预期效果"),
                               ("resources", "可用资源"), ("method", "开展方式"),
                               ("suggestion", "课程建议")):
                _doc_field(blocks, label, item.get(key))
        blocks.append(divider_block())
    return blocks


def _final_blocks(draft, code: str, notice: str = "") -> list[dict]:
    """AI 草稿渲染成结构化块；部长“需修改”的自由文本走宽松 Markdown。"""
    if code in draft.drafts and draft.final_text.get(code) == draft.draft_text.get(code):
        blocks: list[dict] = []
        if notice:
            blocks.append(text_block(notice))
        blocks.extend(_result_doc_blocks(draft.drafts[code], code))
        return blocks
    return markdown_blocks(draft.final_text.get(code, ""))


class Workflow2:
    def __init__(self, *, store: Workflow2Store, preparer: MaterialPreparer,
                 organization: OrganizationCache, prompts: PromptRepository | None,
                 tables: Workflow2Tables, prompt_service: PromptService,
                 messages: MessageService, documents: CloudDocsService,
                 schedules: ScheduleConfig, archive_parent: str,
                 notifications: NotificationPolicy | None = None,
                 retry_interval_minutes: int = 60, retry_limit: int = 3):
        self.store, self.preparer, self.organization = store, preparer, organization
        self.prompts, self.tables, self.prompt_service = prompts, tables, prompt_service
        self.messages, self.documents = messages, documents
        self.schedules, self.archive_parent = schedules, archive_parent
        self.notifications = notifications
        # 阶段分析凌晨启动，失败就整点重来；初次之外最多再重试 retry_limit 次。
        self.retry_interval_minutes = retry_interval_minutes
        self.retry_limit = retry_limit
        self.lock = threading.RLock()

    def _schedule(self, kind: str):
        if kind not in SCHEDULES:
            raise ValueError("未知的 Workflow2 周期")
        return self.schedules.workflows[SCHEDULES[kind]]

    def _analysis_date_millis(self, run: CycleRun) -> int:
        if run.kind != "stage":
            return _write_date_millis()
        offset = int(self._schedule(run.kind).options.get("analysis_date_offset_days", 0))
        return date_millis(run.scheduled_date + timedelta(days=offset))

    def _required_tables(self, kind: str) -> list[str]:
        if kind == "stage":
            return ["stage_analysis", "stage_confirmation", "stage_report"]
        if kind == "monthly":
            return ["monthly_department_analysis",
                             "monthly_department_confirmation", "monthly_department_report"]
        return ["team_analysis"]

    def _check_ready(self, kind: str) -> None:
        self.tables.require(self._required_tables(kind))
        if kind != "weekly" and not str(self._schedule(kind).options.get("form_url", "")).strip():
            raise ValueError(f"{kind} 确认表单链接尚未配置")
        if not self.archive_parent:
            raise ValueError("缺少云文档归档父文件夹 Token")

    def start(self, kind: str, scheduled_date: date) -> CycleRun:
        with self.lock:
            schedule = self._schedule(kind)
            start, end = material_window(schedule, scheduled_date)
            if end >= utc_now().astimezone(SHANGHAI).date():
                raise ValueError("只能分析已经结束的完整自然日")
            run_id = str(uuid5(NAMESPACE_URL, f"recordhub-workflow2:{kind}:{scheduled_date}"))
            run = self.store.load(run_id)
            if run is None:
                now = utc_now()
                run = CycleRun(run_id=run_id, kind=kind, scheduled_date=scheduled_date,
                               start_date=start, end_date=end, created_at=now, updated_at=now)
                self.store.save(run)
            logger.info("workflow2_start kind=%s date=%s run_id=%s",
                        kind, scheduled_date, run_id)
            return self._advance(run)

    def resume(self, run_id: str) -> CycleRun:
        with self.lock:
            run = self.store.load(run_id)
            if run is None:
                raise ValueError("Workflow2 任务不存在")
            logger.info("workflow2_resume run_id=%s", run_id)
            return self._advance(run)

    def _retry_due(self, run: CycleRun, now: datetime) -> bool:
        """失败/被阻塞的任务按小时退避；等确认、等重跑的不受限制。"""
        if run.status not in {"BLOCKED", "FAILED"}:
            return True
        if run.retry_count > self.retry_limit:
            return False
        return run.retry_after is None or run.retry_after <= now

    def _schedule_retry(self, run: CycleRun) -> None:
        run.retry_count += 1
        if run.retry_count <= self.retry_limit:
            run.retry_after = utc_now() + timedelta(minutes=self.retry_interval_minutes)
            return
        run.retry_after = None
        logger.error("workflow2_retry_exhausted run_id=%s kind=%s attempts=%d error=%s",
                     run.run_id, run.kind, run.retry_count, run.last_error or "-")

    def _start_due(self, kind: str, day: date, now: datetime) -> bool:
        hour, minute = map(int, self._schedule(kind).time.split(":"))
        return now >= datetime.combine(day, time(hour, minute), SHANGHAI)

    def recover(self) -> list[CycleRun]:
        now = utc_now()
        today = now.astimezone(SHANGHAI).date()
        activation = self.store.activate(today)
        # 该跑却没跑起来的补上：定时器错过计划时刻（服务不在、4 点整在忙）不会自己补响，
        # 而"每 3 天"的判据让第二天也不会补 —— 不补就是那一轮彻底没了，还不报警。
        for kind in CODES:
            # 恢复只补已经错过的计划，不在午夜提前启动当天的周期。
            if not self._start_due(kind, today, now):
                continue
            run_id = str(uuid5(NAMESPACE_URL, f"recordhub-workflow2:{kind}:{today}"))
            if self.store.load(run_id) is not None:
                continue
            try:
                self.scheduled(kind, today)
            except Exception:
                logger.exception("workflow2_catch_up_failed kind=%s date=%s", kind, today)
        results = [self.resume(run.run_id) for run in self.store.pending()
                   if (run.kind != "stage" or run.start_date > activation)
                   and self._start_due(run.kind, run.scheduled_date, now)
                   and self._retry_due(run, now)]
        # Recovery advances processing only; missed notifications require the admin API.
        logger.debug("workflow2_recover resumed=%d", len(results))
        return results

    def retry_blocked(self, kind: str = "all") -> list[CycleRun]:
        """管理员手动重试卡住的周期任务：无视自动重试预算，只补没完成的部分。

        已出草稿的技能/部门会跳过，所以重跑不会重复调用模型；顺带把重试预算清零，
        以后再卡住时自动重试能重新生效。
        """
        if kind != "all" and kind not in CODES:
            raise ValueError(f"未知的周期类型：{kind}")
        with self.lock:
            retried: list[CycleRun] = []
            for run in self.store.pending():
                if kind != "all" and run.kind != kind:
                    continue
                if run.status not in {"BLOCKED", "FAILED"}:
                    continue
                run.retry_count = 0
                run.retry_after = None
                self.store.save(run)
                logger.info("workflow2_manual_retry run_id=%s kind=%s date=%s error=%s",
                            run.run_id, run.kind, run.scheduled_date, run.last_error or "-")
                retried.append(self.resume(run.run_id))
            return retried

    def scheduled(self, kind: str, today: date) -> CycleRun | None:
        schedule = self._schedule(kind)
        if kind == "stage":
            activation = self.store.activate(today)
            # The activation day may be partial. Count complete days after it,
            # then use the first reporting day as the interval anchor.
            first = activation + timedelta(
                days=int(schedule.options["startup_wait_complete_days"]) + 1)
            if (not schedule.enabled or today < first or
                    (today - first).days % int(schedule.options["every_days"])):
                return None
        elif not due(schedule, today):
            return None
        return self.start(kind, today)

    def _cutoff(self, run: CycleRun) -> datetime:
        options = self._schedule(run.kind).options
        days = int(options.get("confirmation_days", 1))
        hour, minute = map(int, str(options.get("confirmation_time", "12:00")).split(":"))
        return datetime.combine(run.scheduled_date + timedelta(days=days),
                                time(hour, minute), SHANGHAI)

    def _notification_due(self, run: CycleRun) -> bool:
        value = self._schedule(run.kind).options.get("notify_at")
        if not value:
            return True
        hour, minute = map(int, str(value).split(":"))
        scheduled = datetime.combine(run.scheduled_date, time(hour, minute), SHANGHAI)
        return utc_now().astimezone(SHANGHAI) >= scheduled

    def _report_due(self, run: CycleRun) -> bool:
        """向负责人推报告的时刻：配了 report_notify_at 就用它，否则沿用 notify_at。"""
        options = self._schedule(run.kind).options
        value = options.get("report_notify_at") or options.get("notify_at")
        if not value:
            return True
        hour, minute = map(int, str(value).split(":"))
        scheduled = datetime.combine(run.scheduled_date + timedelta(
            days=int(options.get("report_notify_days", 0))), time(hour, minute), SHANGHAI)
        return utc_now().astimezone(SHANGHAI) >= scheduled

    def notify_due(self, kind: str, scheduled_date: date) -> None:
        with self.lock:
            run_id = str(uuid5(NAMESPACE_URL,
                               f"recordhub-workflow2:{kind}:{scheduled_date}"))
            run = self.store.load(run_id)
            if run is None or not self._notification_due(run):
                return
            organization = self.organization.get()
            people = organization.person_map()
            # 阶段分析凌晨就跑完了，但报喜要等 notify_at（08:00）：
            # _departments 里那次即时通知被 _notification_due 挡住，这里补发。
            if kind in {"stage", "monthly"} and utc_now().astimezone(SHANGHAI) < self._cutoff(run):
                for draft in run.departments.values():
                    minister = people.get(draft.minister_id)
                    if (draft.analysis_published and minister and minister.open_id and
                            not set(CODES[kind]) <= set(draft.final_text)):
                        self._notify_minister(run, draft.department_id, minister.open_id)
            if (kind == "stage" and
                    utc_now().astimezone(SHANGHAI) >= self._cutoff(run) and
                    run.status != "COMPLETED"):
                # 截止时间已过：先把 AI 草稿定稿、生成文档，再推给负责人；
                # 不能干等那 15 分钟一轮的恢复，否则 22:00 这一刻可能还没定稿。
                run = self.resume(run.run_id)
            if (self._report_due(run) and run.status == "COMPLETED" and
                    set(CODES[run.kind]) <= set(run.document_urls)):
                leader = people.get(organization.team_leader_id or "")
                if leader and leader.open_id:
                    self._notify_leader(run, leader.open_id)

    def _notify_minister(self, run: CycleRun, department_id: str, open_id: str) -> None:
        self._notify(run, f"minister:{department_id}", open_id,
            f"【{run.start_date} 至 {run.end_date} 周期分析待确认】\n"
            f"请核对本部门分析并填写确认表：{self._schedule(run.kind).options['form_url']}")

    def _notify_leader(self, run: CycleRun, open_id: str) -> None:
        self._notify(run, "leader", open_id,
            "【周期分析报告已生成】\n" + "\n".join(
                f"{NAMES[code]}：{run.document_urls[code]}" for code in CODES[run.kind]))

    @workflow_diagnostics("workflow2")
    def _advance(self, run: CycleRun) -> CycleRun:
        if run.status == "COMPLETED":
            logger.debug("workflow2_already_completed run_id=%s", run.run_id)
            return run
        try:
            self._check_ready(run.kind)
            organization = self.organization.get()
            if organization.anomalies:
                raise ValueError("组织关系异常：" + "；".join(organization.anomalies))
            if run.kind == "weekly":
                self._weekly(run, organization)
            else:
                if utc_now() >= self._cutoff(run):
                    self._freeze(run, organization)
                else:
                    self._departments(run, organization)
                    run.status = "WAITING_CONFIRMATION"
                    self.store.save(run)
                    logger.info("workflow2_waiting_confirmation run_id=%s kind=%s",
                                run.run_id, run.kind)
                    return run
            self._documents(run, organization)
            run.status, run.last_error = "COMPLETED", None
            run.completed_at = utc_now()
            self.store.save(run)
            logger.info("workflow2_completed run_id=%s kind=%s", run.run_id, run.kind)
        except ValueError as exc:
            run.status, run.last_error = "BLOCKED", error_summary(exc)
            self._schedule_retry(run)
            self.store.save(run)
            logger.exception("workflow2_blocked run_id=%s kind=%s date=%s error=%s",
                             run.run_id, run.kind, run.scheduled_date, error_summary(exc))
        except Exception as exc:
            run.status, run.last_error = "FAILED", error_summary(exc)
            self._schedule_retry(run)
            self.store.save(run)
            logger.exception("workflow2_failed run_id=%s kind=%s", run.run_id, run.kind)
        return run

    def _freeze(self, run, organization):
        """Close each department independently; absence is report content."""
        if run.frozen_at is not None:
            return
        for department in organization.departments:
            if not department.active or not department.minister_id:
                continue
            draft = run.departments.setdefault(department.department_id, DepartmentResult(
                department_id=department.department_id, minister_id=department.minister_id))
            try:
                self._confirmation(run, draft, organization)
            except Exception as exc:
                run.issues[f"confirmation:{department.department_id}"] = error_summary(exc)
            for code in CODES[run.kind]:
                draft.final_text.setdefault(code, draft.draft_text.get(code) or
                    f"生成错误：{NAMES[code]} 在截止前未生成；{run.last_error or '无可用结果'}")
        run.frozen_at = utc_now()
        self.store.save(run)

    def _departments(self, run: CycleRun, organization: Organization) -> None:
        people = organization.person_map()
        departments = [item for item in organization.departments if item.active and item.minister_id]
        if not departments:
            raise ValueError("没有有效的部门及部长")
        runner = SkillRunner(self.prompt_service, self.prompts)
        for department in sorted(departments, key=lambda item: item.department_id):
            minister = people[department.minister_id]
            if not minister.active or not minister.open_id:
                raise ValueError(f"部门 {department.department_id} 的部长无有效 OpenID")
            draft = run.departments.get(department.department_id)
            if draft is None:
                draft = DepartmentResult(department_id=department.department_id,
                                         minister_id=minister.person_id)
                run.departments[department.department_id] = draft
                self.store.save(run)
            if set(CODES[run.kind]) <= set(draft.final_text):
                continue
            allow_rebuild = True
            # 阶段分析（S04）只汇总日志原文，不等人工评价定稿；月报/周报要用定稿评价。
            with_evaluations = run.kind != "stage"
            group_materials = [self.preparer.prepare(
                run_id=run.run_id, start=run.start_date, end=run.end_date,
                department_ids=[department.department_id], person_ids=group,
                allow_rebuild=allow_rebuild, allow_partial=True,
                with_evaluations=with_evaluations)
                for group in self._analysis_groups(run.kind, department, organization)]
            for group_material in group_materials:
                if not group_material.input.scope.complete:
                    raise ValueError("；".join(group_material.input.scope.missing_sources))
            omitted = {source for material in group_materials
                       for source in material.input.scope.omitted_sources}
            notice = _partial_notice(omitted)
            if notice:
                run.issues[f"materials:{department.department_id}"] = notice.strip()
                self.store.save(run)
            for code in CODES[run.kind]:
                if code not in draft.drafts:
                    if run.kind == "stage":
                        result = runner.run(code, group_materials[0].input,
                            user_id=minister.person_id)
                        draft.drafts[code] = result.model_dump(mode="json")
                    else:
                        items: list[dict] = []
                        for group_material in group_materials:
                            result = runner.run(code, group_material.input,
                                user_id=minister.person_id)
                            dump = result.model_dump(mode="json")
                            items.extend((dump.get("content") or {}).get("items", []))
                        draft.drafts[code] = {"content": {"items": items}}
                    draft.draft_text[code] = notice + _result_text(
                        draft.drafts[code], code, organization,
                        [department.department_id])
                    self.store.save(run)
            task_id = f"{run.run_id}:{department.department_id}"
            common = {"minister_ref": [{"id": minister.open_id}],
                      "analysis_date": self._analysis_date_millis(run)}
            if run.kind == "stage":
                self.tables.upsert("stage_analysis", task_id,
                    {**common, "progress": draft.draft_text["S04"]})
            else:
                self.tables.upsert("monthly_department_analysis", task_id,
                    {**common, **{RESULT_COLUMNS[code]: draft.draft_text[code]
                                  for code in CODES[run.kind]}})
            draft.analysis_published = True
            self.store.save(run)
            if (utc_now().astimezone(SHANGHAI) < self._cutoff(run) and
                    self._notification_due(run)):
                self._notify_minister(run, department.department_id, minister.open_id)
            self._confirmation(run, draft, organization)

    @staticmethod
    def _analysis_groups(kind: str, department,
                         organization: Organization) -> list[list[str] | None]:
        """One LLM material batch per group; None means the whole department.

        Monthly runs analyze per backbone group (the backbone plus the students
        whose leader they are); students without a backbone leader form one
        trailing group under the minister's review.
        """
        if kind == "stage":
            return [None]
        students = [person for person in organization.persons if person.active
                    and person.role in {"基层学生", "骨干学生"}
                    and person.department_id == department.department_id]
        groups: list[list[str]] = []
        assigned: set[str] = set()
        for core in [person for person in students if person.role == "骨干学生"]:
            members = [core.person_id] + [
                person.person_id for person in students
                if person.role == "基层学生" and person.leader_id == core.person_id]
            assigned.update(members)
            groups.append(members)
        orphans = [person.person_id for person in students if person.person_id not in assigned]
        if orphans:
            groups.append(orphans)
        return groups or [None]

    def _confirmation(self, run: CycleRun, draft: DepartmentResult,
                      organization: Organization) -> None:
        codes = CODES[run.kind]
        if run.frozen_at is not None:
            return
        table_name = "stage_confirmation" if run.kind == "stage" else "monthly_department_confirmation"
        table = self.tables.table(table_name)
        f = table.fields
        person = organization.person_map()[draft.minister_id]
        task_id = f"{run.run_id}:{draft.department_id}"
        matches = []
        legacy_matches = []
        shared_minister = sum(item.minister_id == draft.minister_id
                              for item in run.departments.values()) > 1
        for row in self.tables.bitable.list_records(table.table_id):
            values = record_fields(row)
            supplied_id = scalar(values.get(f["task_id"]))
            if supplied_id and supplied_id != task_id:
                continue
            identities = {draft.minister_id, person.name, person.open_id,
                          person.source_record_id}
            author_key = "submitted_by" if run.kind == "stage" else "minister_ref"
            if not identities.intersection(references(values.get(f[author_key]))):
                continue
            if supplied_id == task_id:
                matches.append(row)
                continue
            # Without a task ID the minister alone cannot identify a department.
            if shared_minister:
                continue
            try:
                recorded_day = field_datetime(values.get(f["analysis_date"])).astimezone(SHANGHAI).date()
            except ValueError:
                continue
            # The form date is the day the minister submits it. A confirmation
            # may arrive on the next day before the configured cutoff.
            if run.scheduled_date <= recorded_day <= self._cutoff(run).date():
                legacy_matches.append(row)
        # An explicit task ID takes precedence over nearby unassigned forms.
        matches = matches or legacy_matches
        if len(matches) > 1:
            raise ValueError(f"部长 {draft.minister_id} 的周期确认记录重复")
        late = utc_now().astimezone(SHANGHAI) >= self._cutoff(run)
        row = matches[0] if matches else None
        row_submitted_late = late
        if row is not None:
            timestamp = row.get("last_modified_time") or row.get("created_time")
            if timestamp:
                row_submitted_late = field_datetime(timestamp).astimezone(SHANGHAI) >= self._cutoff(run)
        if row and row_submitted_late:
            run.issues.setdefault(f"late:{draft.department_id}", "确认截止后提交，保留 AI 草稿")
        elif row:
            values = record_fields(row)
            if not scalar(values.get(f["task_id"])):
                self.tables.bitable.update_record(table.table_id, str(row["record_id"]),
                                                  {f["task_id"]: task_id})
            draft.confirmation_record_id = str(row["record_id"])
            for code in codes:
                decision_key, edited_key = (("decision", "edited") if run.kind == "stage"
                                            else CONFIRM_COLUMNS[code])
                decision = scalar(values.get(f[decision_key])).strip()
                if decision == "确认无误":
                    draft.final_text[code] = draft.draft_text.get(code) or "生成错误：AI 内容缺失"
                elif decision == "需修改":
                    edited = scalar(values.get(f[edited_key])).strip()
                    if not edited:
                        raise ValueError(f"{code} 选择需修改但未填写修改内容")
                    draft.final_text[code] = edited
                elif decision:
                    raise ValueError(f"{code} 确认选项无效：{decision}")
        if late:
            for code in codes:
                if code not in draft.final_text:
                    draft.final_text[code] = draft.draft_text.get(code) or "生成错误：AI 内容缺失"
                    if code not in draft.unconfirmed:
                        draft.unconfirmed.append(code)
        self.store.save(run)
    def _weekly(self, run: CycleRun, organization: Organization) -> None:
        leader_id = organization.team_leader_id
        if not leader_id:
            raise ValueError("团队负责人缺失")
        people = organization.person_map()
        departments = [item for item in organization.departments
                       if item.active and item.minister_id]
        department_ids = sorted(item.department_id for item in departments)
        runner = SkillRunner(self.prompt_service, self.prompts)

        # ① team reference pass over the whole organization.
        team_material = self.preparer.prepare(run_id=run.run_id, start=run.start_date,
                                             end=run.end_date, department_ids=department_ids,
                                             allow_rebuild=True, allow_partial=True)
        if not team_material.input.scope.complete:
            raise ValueError("；".join(team_material.input.scope.missing_sources))
        weekly_notice = _partial_notice(set(team_material.input.scope.omitted_sources))
        if weekly_notice:
            run.issues["materials:weekly"] = weekly_notice.strip()
            self.store.save(run)
        for code in CODES["weekly"]:
            if code not in run.weekly_reference:
                result = runner.run(code, team_material.input, user_id=leader_id)
                run.weekly_reference[code] = result.model_dump(mode="json")
                self.store.save(run)

        # ② per-department analysis with the department's own skill config.
        for department_id in department_ids:
            bucket = run.department_results.setdefault(department_id, {})
            if set(CODES["weekly"]) <= set(bucket):
                continue
            department_material = self.preparer.prepare(run_id=run.run_id,
                start=run.start_date, end=run.end_date,
                department_ids=[department_id], allow_rebuild=True,
                allow_partial=True)
            if not department_material.input.scope.complete:
                raise ValueError("；".join(department_material.input.scope.missing_sources))
            minister = people[organization.department_map()[department_id].minister_id]
            for code in CODES["weekly"]:
                if code not in bucket:
                    result = runner.run(code, department_material.input,
                        user_id=minister.person_id)
                    bucket[code] = result.model_dump(mode="json")
                    self.store.save(run)

        # ③ summary pass merging the reference and department drafts.
        names = organization.department_map()
        for code in CODES["weekly"]:
            if code in run.team_results:
                continue
            sections = [DepartmentAnalysis(
                department_name=names[department_id].name,
                items=((run.department_results[department_id].get(code) or {})
                       .get("content") or {}).get("items", []))
                for department_id in department_ids]
            reference_items = ((run.weekly_reference.get(code) or {})
                               .get("content") or {}).get("items", [])
            data = WeeklySummaryInput(skill_code=code, start_date=run.start_date,
                end_date=run.end_date, reference_items=reference_items, departments=sections)
            allowed_names = {name
                             for section in [reference_items, *(s.items for s in sections)]
                             for item in section
                             for key in ("contributors", "proposers")
                             for name in (item.get(key) or [])}

            def validate(output, *, _names=allowed_names) -> None:
                for item in output.items:
                    values = getattr(item, "contributors", None)
                    if values is not None and not set(values) <= _names:
                        raise ValueError("总结的 contributors 超出本次输入人员")
            output = self.prompt_service.execute(prompt_code=f"{code}10",
                template=Path("prompts/S10.txt").read_text(encoding="utf-8").strip(),
                input_data=data, output_model=SUMMARY_MODELS[code],
                semantic_validator=validate, array_root=True)
            run.team_results[code] = {"content": output.model_dump(mode="json")}
            self.store.save(run)
    @stage_diagnostics("generate_documents")
    def _documents(self, run: CycleRun, organization: Organization) -> None:
        leader = organization.person_map().get(organization.team_leader_id or "")
        if leader is None or not leader.open_id:
            raise ValueError("团队负责人缺少 OpenID")
        parent = self.documents.find_child(self.archive_parent, "workflow2", "folder")
        parent = parent or self.documents.create_folder(self.archive_parent, "workflow2")
        folder_name = run.scheduled_date.isoformat()
        folder = self.documents.find_child(parent, folder_name, "folder")
        folder = folder or self.documents.create_folder(parent, folder_name)
        for code in CODES[run.kind]:
            title = f"{run.scheduled_date:%m-%d} 团队{NAMES[code]}"
            blocks = [text_block(title, heading=1),
                      text_block(f"-分析范围：{run.start_date} 至 {run.end_date}")]
            if run.kind == "weekly" and run.issues.get("materials:weekly"):
                blocks.append(text_block(run.issues["materials:weekly"]))
            if run.kind == "weekly":
                blocks.append(text_block("团队总结", heading=2))
                blocks.extend(_result_doc_blocks(run.team_results[code], code))
                names = organization.department_map()
                for department_id, bucket in sorted(run.department_results.items()):
                    department = names[department_id]
                    minister = organization.person_map()[department.minister_id]
                    blocks.append(divider_block())
                    blocks.append(text_block(f"{minister.name}老师部门（{department.name}）", heading=2))
                    blocks.extend(_result_doc_blocks(bucket.get(code) or {}, code))
                blocks.append(divider_block())
                blocks.append(text_block("团队参考版", heading=2))
                blocks.extend(_result_doc_blocks(run.weekly_reference.get(code) or {}, code))
            for department_id, draft in sorted(run.departments.items()):
                department = organization.department_map()[department_id]
                minister = organization.person_map()[draft.minister_id]
                blocks.append(divider_block())
                blocks.append(text_block(f"{minister.name}老师部门（{department.name}）", heading=2))
                if code in draft.unconfirmed:
                    blocks.append(text_block("-确认状态：人工未确认，采用 AI 草稿"))
                notice = run.issues.get(f"materials:{department_id}", "")
                blocks.extend(_final_blocks(draft, code, notice))
            self._ensure_document(run, folder, code, title, blocks)
        for department_id, draft in sorted(run.departments.items()):
            department = organization.department_map()[department_id]
            minister = organization.person_map()[draft.minister_id]
            links = {}
            for code in CODES[run.kind]:
                title = f"{run.scheduled_date:%m-%d} {department.name}{NAMES[code]}"
                blocks = [text_block(title, heading=1),
                          text_block(f"-分析范围：{run.start_date} 至 {run.end_date}")]
                if code in draft.unconfirmed:
                    blocks.append(text_block("-确认状态：人工未确认，采用 AI 草稿"))
                notice = run.issues.get(f"materials:{department_id}", "")
                blocks.extend(_final_blocks(draft, code, notice))
                links[code] = self._ensure_document(
                    run, folder, f"{code}:{department_id}", title, blocks)
            common = {"minister_ref": [{"id": minister.open_id}],
                      "analysis_date": _write_date_millis()}
            task_id = f"{run.run_id}:{department_id}"
            if run.kind == "stage":
                self.tables.upsert("stage_report", task_id,
                    {**common, "progress": self.tables.url_value("stage_report", "progress", links["S04"], NAMES["S04"])})
            else:
                self.tables.upsert("monthly_department_report", task_id,
                    {**common, **{RESULT_COLUMNS[code]: self.tables.url_value(
                        "monthly_department_report", RESULT_COLUMNS[code], links[code], NAMES[code])
                        for code in CODES["monthly"]}})
        if run.kind == "weekly":
            self.tables.upsert("team_analysis", run.run_id,
                {"analysis_date": _write_date_millis(),
                 "technology_s08": self.tables.url_value(
                     "team_analysis", "technology_s08", run.document_urls["S08"], NAMES["S08"]),
                 "training_s09": self.tables.url_value(
                     "team_analysis", "training_s09", run.document_urls["S09"], NAMES["S09"])})
        elif run.kind == "stage":
            self.tables.upsert("stage_report", run.run_id + ":TEAM",
                {"minister_ref": [{"id": leader.open_id}],
                 "analysis_date": _write_date_millis(),
                 "progress": self.tables.url_value("stage_report", "progress", run.document_urls["S04"], NAMES["S04"])})
        elif run.kind == "monthly":
            self.tables.upsert("monthly_department_report", run.run_id + ":TEAM",
                {"minister_ref": [{"id": leader.open_id}],
                 "analysis_date": _write_date_millis(),
                 **{RESULT_COLUMNS[code]: self.tables.url_value(
                     "monthly_department_report", RESULT_COLUMNS[code], run.document_urls[code], NAMES[code])
                    for code in CODES["monthly"]}})
        # 负责人那份报告等 report_notify_at（22:00）再推；到点由 notify_due 负责发。
        if self._report_due(run):
            self._notify_leader(run, leader.open_id)

    def _ensure_document(self, run: CycleRun, folder: str, key: str,
                         title: str, blocks: list[dict]) -> str:
        token = run.document_tokens.get(key)
        if not token:
            token = self.documents.find_child(folder, title, "docx")
            token = token or self.documents.create_document(folder, title)
            run.document_tokens[key] = token
            self.store.save(run)
        existing = [block for block in self.documents.list_blocks(token)
                    if DailyDocuments._block_content(block).strip()]
        expected = [block for block in blocks if block.get("block_type") != 22]
        if len(existing) > len(expected) or any(
            DailyDocuments._block_signature(actual) != DailyDocuments._block_signature(wanted)
            for actual, wanted in zip(existing, expected)):
            raise ValueError(f"云文档 {title} 已被修改，停止自动覆盖")
        tail = []
        seen = 0
        for block in blocks:
            if block.get("block_type") != 22:
                if seen >= len(existing):
                    tail.append(block)
                seen += 1
            elif seen >= len(existing):
                tail.append(block)
        if tail:
            self.documents.append_blocks(token, tail)
        url = f"https://feishu.cn/docx/{token}"
        run.document_urls[key] = url
        self.store.save(run)
        return url

    @workflow_diagnostics("workflow2")
    def _notify(self, run: CycleRun, key: str, open_id: str, message: str) -> None:
        if key in run.notification_ids:
            record_outcome(f"workflow2:{run.run_id}:{key}", "already_sent")
            return
        business_key = f"workflow2:{run.run_id}:{key}"
        def send():
            with diagnostic_context(notification_key=business_key, recipient=open_id):
                return self.messages.send_text(open_id, message, idempotency_key=business_key)
        try:
            if getattr(self, "notifications", None) is None:
                result = send()
            else:
                result = self.notifications.deliver(
                    key=business_key, target_date=run.scheduled_date, created_at=run.created_at,
                    send_date=(self._cutoff(run).date() if key == "leader" and run.kind != "weekly"
                               else run.scheduled_date), receive_id=open_id, send=send,
                    message=message, workflow="workflow2", kind=run.kind,
                    message_type="review" if key.startswith("minister:") else "report")
                if result is None:
                    return
        except Exception as exc:
            logger.exception("workflow2_notification_failed run_id=%s date=%s key=%s to=%s",
                             run.run_id, run.scheduled_date, key, open_id)
            record_outcome(business_key, "failed", error=error_summary(exc))
            return
        message_id = scalar(result.get("message_id"))
        if not message_id:
            raise ValueError("周期通知没有返回 message_id")
        run.notification_ids[key] = message_id
        self.store.save(run)
        logger.info("workflow2_notification_sent run_id=%s key=%s", run.run_id, key)

    @workflow_diagnostics("workflow2")
    def send_notifications(self, scheduled_date: date, message_type: str = "all",
                           kind: str = "all", confirm_unsent: bool = False) -> dict:
        with self.lock, manual_notifications(scheduled_date, confirm_unsent) as results:
            runs = [run for run in self.store.all_runs()
                    if run.scheduled_date == scheduled_date and
                    (kind == "all" or run.kind == kind)]
            if not runs:
                raise LookupError(f"{scheduled_date} 没有匹配的周期任务")
            organization = self.organization.get()
            people = organization.person_map()
            for run in runs:
                if message_type in {"all", "review"} and run.kind != "weekly":
                    for draft in run.departments.values():
                        key = f"workflow2:{run.run_id}:minister:{draft.department_id}"
                        minister = people.get(draft.minister_id)
                        if not draft.analysis_published:
                            record_outcome(key, "not_ready", reason="analysis_not_published")
                        elif minister is None or not minister.open_id:
                            record_outcome(key, "failed", error="部长缺少 OpenID")
                            logger.error("manual_notification_missing_recipient date=%s key=%s", scheduled_date, key)
                        else:
                            self._notify_minister(run, draft.department_id, minister.open_id)
                if message_type in {"all", "report"}:
                    key = f"workflow2:{run.run_id}:leader"
                    leader = people.get(organization.team_leader_id or "")
                    if not set(CODES[run.kind]) <= set(run.document_urls):
                        record_outcome(key, "not_ready", reason="documents_not_ready")
                    elif leader is None or not leader.open_id:
                        record_outcome(key, "failed", error="团队负责人缺少 OpenID")
                        logger.error("manual_notification_missing_recipient date=%s key=%s", scheduled_date, key)
                    else:
                        self._notify_leader(run, leader.open_id)
            return {"workflow": "workflow2", "target_date": scheduled_date.isoformat(),
                    "messages": list({item["key"]: item for item in results}.values())}

    def handle_confirmation(self, record_id: str) -> dict:
        # Record confirmation only. Model generation and documents have their
        # own appointments; an edited form must never restart either of them.
        updated = []
        with self.lock:
            organization = self.organization.get()
            for run in self.store.pending():
                if run.kind not in {"stage", "monthly"} or run.frozen_at or utc_now() >= self._cutoff(run):
                    continue
                for draft in run.departments.values():
                    self._confirmation(run, draft, organization)
                updated.append(run.run_id)
        logger.info("workflow2_confirmation_event record_id=%s checked=%d",
                    record_id, len(updated))
        return {"record_id": record_id, "checked_runs": updated}

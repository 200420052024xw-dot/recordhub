"""Daily Workflow1 policy. Feishu and DeepSeek transports live outside src."""

from __future__ import annotations

import logging
import threading
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from data import FileStateStore, utc_now
from data.repositories import LogRepository, OrganizationCache
from workflow1.snapshot import SnapshotBuilder
from llm import LLMError, PromptRepository, PromptService
from schema import WorkLog, WorkflowRun
from workflow1.models import ConfirmationRequest, DailySnapshot, LogPromptInput, LogPromptOutput, UnitStatus, WorkflowStatus
from workflow1.workbuddy_inbox import LogSubmitService
from tool.feishu import MessageService
from tool.errors import FeishuApiError
from tool.diagnostics import error_summary, workflow_diagnostics, diagnostic_context
from tool.notifications import (NotificationPolicy, manual_delivery,
                                manual_notifications, record_outcome, confirmed_unsent)
from workflow1.documents import DailyDocuments
from workflow1.evaluations import (AiEvaluationRepository, HumanEvaluationRepository,
                                   review_day_datetime)
from workflow1.reports import WorkflowReportRepository
from workflow1.submission_statistics import SubmissionStatisticsRepository

logger = logging.getLogger(__name__)

# 通知称呼：部长/团队负责人称「老师」，骨干称「同学」；其余角色不加后缀。
ROLE_TITLES = {"部长": "老师", "团队负责人": "老师", "骨干学生": "同学"}


def date_cn(target_date: date) -> str:
    return f"{target_date.month}月{target_date.day}日"


def greeting(person) -> str:
    return f"{person.name}{ROLE_TITLES.get(person.role, '')}，"


# 评价输出解析不了时，把模型这次的原始文本原样放进「肯定之处_AI」，改进那栏指向它。
# 内容其实已经生成，丢掉的话当天就完全没有评价，留下至少能人工核对。
UNPARSED_IMPROVEMENT = "详情见「肯定之处_AI」"
# 接口/网络失败没有文本可留：延后重试，最多三次；仍失败就写这句并报管理员。
# 半夜这批 AI 评价不着急，间隔留足一小时，让上游抖动有时间自己恢复。
# 实际取值由 RECORDHUB_LLM_RETRY_INTERVAL_MINUTES / _LIMIT 决定，这里是默认值。
FAILED_EVALUATION_TEXT = "AI 评价失败，请人工补评"
RETRY_LIMIT = 3
RETRY_DELAY = timedelta(minutes=60)
# 整天没跑完（快照没建、发布失败、进程被杀……）时，逐小时重试的回看窗口：
# 只碰最近两天，别去翻陈年老账（重跑旧日期会补发过期的通知）。
INCOMPLETE_RETRY_WINDOW_DAYS = 2
# 已经标过"需人工补评"的日期不再自动折腾，等人来处理。
MANUAL_REVIEW_KEY = "workflow1:manual-review"


def _unparsed_output(exc: Exception) -> str:
    """取回模型这次返回的原始文本；接口/网络错误没有文本，返回空串。"""
    for item in (exc, exc.__cause__):
        raw = getattr(item, "raw", None)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


class Workflow1:
    def __init__(self, *, store: FileStateStore,
                 organization_cache: OrganizationCache,
                 log_repository: LogRepository,
                 ai_evaluations: AiEvaluationRepository,
                 human_evaluations: HumanEvaluationRepository,
                 prompt_service: PromptService,
                 messages: MessageService,
                 documents: DailyDocuments,
                 reports: WorkflowReportRepository,
                 statistics: SubmissionStatisticsRepository | None = None,
                 prompt_repository: PromptRepository | None = None,
                 workbuddy: LogSubmitService | None = None,
                 llm_concurrency: int = 3,
                 llm_retry_interval_minutes: int = 60,
                 llm_retry_limit: int = 3,
                 auto_advance_at: str = "12:00",
                 notify_at: str = "08:00",
                 report_notify_at: str = "22:00",
                 minister_review_form_url: str = "https://jwxnd3ayslt.feishu.cn/share/base/form/shrcnaBl2hPcNAs4nNBYI2STpGh",
                 backbone_review_form_url: str = "https://jwxnd3ayslt.feishu.cn/share/base/form/shrcngOWTo1tmzxm4V9blwKA1Dd",
                 prompt_path: str = "prompts/S01.txt",
                 admin_open_id: str = "",
                 notifications: NotificationPolicy | None = None) -> None:
        self.store = store
        self.organization_cache = organization_cache
        self.log_repository = log_repository
        self.ai_evaluations = ai_evaluations
        self.human_evaluations = human_evaluations
        self.prompt_service = prompt_service
        self.prompt_repository = prompt_repository
        self.workbuddy = workbuddy
        self.messages = messages
        self.documents = documents
        self.reports = reports
        self.statistics = statistics
        self.llm_concurrency = llm_concurrency
        self.llm_retry_delay = timedelta(minutes=llm_retry_interval_minutes)
        self.llm_retry_limit = llm_retry_limit
        self.auto_advance_at = auto_advance_at
        self.notify_at = notify_at
        self.report_notify_at = report_notify_at
        self.review_form_urls = {
            "部长": minister_review_form_url,
            "骨干学生": backbone_review_form_url,
        }
        self.prompt_path = Path(prompt_path)
        self.admin_open_id = admin_open_id
        self.notifications = notifications
        self._lock = threading.RLock()

    def _record_issue(self, target_date: date, key: str, reason: str) -> None:
        """Append to the daily ledger; the error handler alerts the administrator."""
        def mutate(snapshot: DailySnapshot) -> bool:
            if key in snapshot.issues:
                return False
            snapshot.issues[key] = reason
            return True

        added = self.store.update_snapshot(target_date, mutate)
        if added:
            logger.error("workflow1_issue_recorded date=%s key=%s reason=%s",
                         target_date, key, reason, exc_info=sys.exc_info()[0] is not None)

    def _flush_workbuddy_inbox(self, target_date: date) -> dict[str, str]:
        """把对话入口收到的日志写回飞书,必须发生在建快照之前。

        写回失败就让异常冒出去(当日 FAILED、可 resume):宁可整日推迟,
        也不能带着缺失的日志建快照——那会把已交的学生写成「未填写日志」。
        """
        if self.workbuddy is None:
            return {}
        written, issues = self.workbuddy.flush(target_date)
        if written:
            logger.info("workflow1_workbuddy_flushed date=%s written=%d",
                        target_date, written)
        return issues

    @workflow_diagnostics("workflow1")
    def refresh_submission_statistics(self, target_date: date) -> dict:
        """Read source logs and publish counts without evaluating or reporting.

        Share the daily lock so reading the inbox cannot race daily startup.
        A historical snapshot supplies its original roster, but is not required.
        """
        with self._lock:
            if self.statistics is None:
                return {"target_date": str(target_date), "status": "disabled",
                        "expected": 0, "written": 0, "issues": {}, "notices": {}}
            try:
                snapshot = self.store.load_check_snapshot(target_date)
                organization = snapshot.organization if snapshot else self.organization_cache.get()
                logs, read_issues = self.log_repository.get_logs_by_date(
                    target_date, organization=organization)
                if self.workbuddy is not None:
                    # Pending submissions are real submissions too. Counting
                    # them does not flush files or start an evaluation workflow.
                    logs = list(logs) + [WorkLog(log_id=item.submission_id,
                        person_id=item.person_id, submitted_at=item.submitted_at,
                        progress=item.progress, difficulties=item.difficulties,
                        reflection=item.reflection, other=item.other)
                        for item in self.workbuddy.inbox.pending(target_date).values()]
                result = self.statistics.publish(target_date, organization, logs)
                # Two read-side outcomes are expected rather than faulty:
                # superseded submissions lose to the last-one-wins rule, and
                # out-of-window ones are refused by design. Neither may turn a
                # complete write into a partial one or wake the administrator,
                # so they are reported as notices at INFO instead.
                result["issues"].update({key: reason for key, reason in read_issues.items()
                                         if not key.endswith((":superseded", ":outside-window"))})
                result["notices"] = {key: reason for key, reason in read_issues.items()
                                     if key.endswith(":outside-window")}
                if result["issues"]:
                    result["status"] = "partial"
                for key, reason in result["issues"].items():
                    logger.error("submission_statistics_issue date=%s key=%s reason=%s",
                                 target_date, key, reason)
                for key, reason in result["notices"].items():
                    logger.info("submission_statistics_notice date=%s key=%s reason=%s",
                                target_date, key, reason)
                logger.info("submission_statistics_finished date=%s expected=%s written=%s status=%s",
                            target_date, result["expected"], result["written"], result["status"])
                return result
            except Exception as exc:
                logger.exception("submission_statistics_failed date=%s", target_date)
                return {"target_date": str(target_date), "status": "failed",
                        "expected": 0, "written": 0, "notices": {},
                        "issues": {"statistics:failed": error_summary(exc)}}

    @workflow_diagnostics("workflow1")
    def start(self, target_date: date) -> WorkflowRun:
        with self._lock:
            run = self.store.get_or_create_workflow(target_date)
            if run.status == WorkflowStatus.COMPLETED:
                return run
            if self._deadline_passed(target_date) and self.store.load_snapshot(target_date):
                self.finalize_pending_confirmations(target_date)
                return self.store.load_workflow(target_date)
            # Statistics neither wait for nor change evaluation/report status.
            if (not getattr(self, "statistics_scheduled", False)
                    and self.store.load_snapshot(target_date) is None):
                self.refresh_submission_statistics(target_date)
            run = self.store.get_or_create_workflow(target_date)
            if run.status == WorkflowStatus.COMPLETED:
                logger.debug("workflow1_already_completed date=%s", target_date)
                return run
            logger.info("workflow1_start date=%s", target_date)
            try:
                snapshot = self.store.load_snapshot(target_date)
                if snapshot is not None and snapshot.schema_version != 2:
                    raise ValueError("旧版 Workflow1 快照不能按新流程续跑；请人工迁移该日期状态")
                pending_issues = self._flush_workbuddy_inbox(target_date)
                if snapshot is None:
                    self.store.set_status(target_date, WorkflowStatus.SNAPSHOT_BUILDING)
                    organization = self.organization_cache.get()
                    if organization.anomalies:
                        raise ValueError("组织关系异常：" + "；".join(organization.anomalies))
                    logs, read_issues = self.log_repository.get_logs_by_date(
                        target_date, organization=organization)
                    self.log_repository.backfill_full_logs(logs)
                    snapshot = SnapshotBuilder().build(
                        workflow_run_id=run.workflow_run_id, target_date=target_date,
                        organization=organization, logs=logs, now=utc_now())
                    self.store.save_snapshot(snapshot)
                    self.store.set_status(target_date, WorkflowStatus.SNAPSHOT_READY)
                    for key, reason in read_issues.items():
                        self._record_issue(target_date, key, reason)
                    for key, reason in pending_issues.items():
                        self._record_issue(target_date, key, reason)
                elif pending_issues:
                    # 已有快照,登记不了 issue;留日志即可,不因为这条重跑整日。
                    for key, reason in pending_issues.items():
                        logger.error("workbuddy_flush_issue date=%s key=%s reason=%s",
                                     target_date, key, reason)
                self._process_logs(target_date)
                snapshot = self.store.load_snapshot(target_date)
                assert snapshot is not None
                mapped, publish_issues = self.ai_evaluations.publish(snapshot)
                for key, reason in publish_issues.items():
                    self._record_issue(target_date, key, reason)
                def save_published(item: DailySnapshot) -> None:
                    for log_id, (record_id, evaluation_id) in mapped.items():
                        item.log_evaluations[log_id].record_id = record_id
                        item.log_evaluations[log_id].evaluation_id = evaluation_id
                    item.evaluations_published = all(
                        state.status not in {UnitStatus.PENDING, UnitStatus.RUNNING}
                        for state in item.log_evaluations.values())
                if mapped or not snapshot.evaluations_published:
                    self.store.update_snapshot(target_date, save_published)
                self.notify_evaluators_if_due(target_date)
                self._reconcile_human(target_date)
                if self._deadline_passed(target_date):
                    self._close_remaining(target_date)
                self.advance(target_date)
            except Exception as exc:
                logger.exception("workflow1_failed date=%s", target_date)
                self.store.set_status(target_date, WorkflowStatus.FAILED, error=error_summary(exc))
            result = self.store.load_workflow(target_date) or run
            logger.info("workflow1_finished date=%s status=%s", target_date, result.status)
            return result

    def resume(self, target_date: date) -> WorkflowRun:
        with self._lock:
            logger.info("workflow1_resume date=%s", target_date)
            self.store.get_or_create_workflow(target_date)
            if self.store.load_snapshot(target_date):
                self.store.update_snapshot(target_date, self._reset_failed)
            return self.start(target_date)

    @staticmethod
    def _reset_failed(snapshot: DailySnapshot) -> None:
        for state in snapshot.log_evaluations.values():
            if state.status in {UnitStatus.FAILED, UnitStatus.RUNNING}:
                state.status = UnitStatus.PENDING
                state.error = None

    def recover_incomplete(self) -> list[WorkflowRun]:
        # 启动恢复不重置 FAILED：只有显式调用 resume（重评接口）才会重跑失败项。
        return [self.start(run.target_date)
                for run in self.store.list_incomplete_workflows()]

    def _process_logs(self, target_date: date) -> None:
        snapshot = self.store.load_snapshot(target_date)
        assert snapshot is not None
        # FAILED 单元只能由 resume 接口显式重置，自动路径永不重评。
        pending = [state for state in snapshot.log_evaluations.values()
                   if state.status in {UnitStatus.PENDING, UnitStatus.RUNNING}]
        if limit := getattr(self, "batch_limit", None):
            pending = pending[:limit]
        if not pending:
            return
        self.store.set_status(target_date, WorkflowStatus.LOGS_RUNNING)
        builtin_template = self.prompt_path.read_text(encoding="utf-8").strip()
        if not builtin_template:
            raise ValueError(f"Workflow1 prompt 为空：{self.prompt_path}")
        logs = {log.log_id: log for log in snapshot.logs}

        def process(log_id: str) -> tuple[str, str, str, str, str | None]:
            state = snapshot.log_evaluations[log_id]
            log = logs[log_id]
            selected = (self.prompt_repository.resolve("S01", state.evaluator_id)
                        if self.prompt_repository and state.evaluator_id else None)
            template = selected.template if selected else builtin_template
            with diagnostic_context(workflow="workflow1", target_date=str(target_date),
                                    log_id=log_id, person_id=state.person_id):
                prompt_input = LogPromptInput(target_date=target_date,
                    person_id=state.person_id, log_id=log_id, log=log.content())
                try:
                    output = self.prompt_service.execute(
                        prompt_code="LOG", template=template,
                        input_data=prompt_input, output_model=LogPromptOutput)
                except LLMError:
                    if selected is None:
                        raise
                    logger.warning(
                        "personal_prompt_failed_fallback workflow=workflow1 log_id=%s "
                        "user_id=%s record_id=%s", log_id, state.evaluator_id,
                        selected.record_id, exc_info=True)
                    output = self.prompt_service.execute(
                        prompt_code="LOG", template=builtin_template,
                        input_data=prompt_input, output_model=LogPromptOutput)
                    selected = None
            return (log_id, output.positive, output.improvement,
                    "TABLE" if selected else "BUILTIN",
                    selected.record_id if selected else None)

        failures = []
        with ThreadPoolExecutor(max_workers=self.llm_concurrency) as pool:
            from contextvars import copy_context
            futures = {pool.submit(copy_context().run, process, state.log_id): state.log_id
                       for state in pending}
            for future in as_completed(futures):
                log_id = futures[future]
                try:
                    _, positive, improvement, prompt_source, prompt_record_id = future.result()
                    def save(item: DailySnapshot) -> None:
                        state = item.log_evaluations[log_id]
                        state.positive_ai = state.positive_final = positive
                        state.improvement_ai = state.improvement_final = improvement
                        state.prompt_source = prompt_source
                        state.prompt_record_id = prompt_record_id
                        state.status = (UnitStatus.WAITING_CONFIRMATION
                            if state.evaluator_id else UnitStatus.CONFIRMED)
                        # 飞书查找引用的“等于今天”按当天 00:00:00 精确比较。
                        # AI 评价统一使用审核日零点；人工确认仍保留真实发生时间。
                        state.evaluated_at = review_day_datetime(target_date)
                        state.ai_evaluated_at = state.evaluated_at
                        if state.status == UnitStatus.CONFIRMED:
                            state.confirmed_at = state.evaluated_at
                        state.error = None
                    self.store.update_snapshot(target_date, save)
                except Exception as exc:
                    failures.append(log_id)
                    unparsed = _unparsed_output(exc)
                    if unparsed:
                        # 模型其实产出了内容，只是没法解析成两个字段：留着比丢掉强。
                        # 这条是 warning 且不带 exc_info，不触发管理员告警。
                        logger.warning("workflow1_log_unparsed date=%s log_id=%s error=%s",
                                       target_date, log_id, error_summary(exc))
                        def degrade(item: DailySnapshot) -> None:
                            state = item.log_evaluations[log_id]
                            state.positive_ai = state.positive_final = unparsed
                            state.improvement_ai = state.improvement_final = UNPARSED_IMPROVEMENT
                            state.status = (UnitStatus.WAITING_CONFIRMATION
                                if state.evaluator_id else UnitStatus.CONFIRMED)
                            state.evaluated_at = review_day_datetime(target_date)
                            state.ai_evaluated_at = state.evaluated_at
                            if state.status == UnitStatus.CONFIRMED:
                                state.confirmed_at = state.evaluated_at
                            state.error = error_summary(exc)
                            state.fallback = "unparsed"
                        self.store.update_snapshot(target_date, degrade)
                        self._record_issue(
                            target_date, f"log:{log_id}:ai-unparsed",
                            f"日志 {log_id} 的 AI 评价无法解析成规定格式，已把模型原始输出"
                            f"原样放进「肯定之处_AI」，改进栏指向它，请人工核对后确认："
                            f"{error_summary(exc)}")
                        continue
                    self._defer_or_give_up(target_date, log_id, exc)
        logger.info("workflow1_logs_evaluated date=%s pending=%d failed=%d",
                    target_date, len(pending), len(failures))

    def _defer_or_give_up(self, target_date: date, log_id: str, exc: Exception) -> None:
        """接口/网络失败的日志评价：延后重试，最多 llm_retry_limit 次；仍失败才报管理员。"""
        snapshot = self.store.load_snapshot(target_date)
        current = snapshot.log_evaluations.get(log_id) if snapshot else None
        attempt = (current.retry_count if current else 0) + 1
        reason = error_summary(exc)
        if attempt <= self.llm_retry_limit:
            retry_after = utc_now() + self.llm_retry_delay
            def defer(item: DailySnapshot) -> None:
                state = item.log_evaluations[log_id]
                state.status = UnitStatus.FAILED
                state.retry_count = attempt
                state.retry_after = retry_after
                state.error = reason
            self.store.update_snapshot(target_date, defer)
            logger.warning("workflow1_log_evaluation_deferred date=%s log_id=%s "
                           "attempt=%d/%d retry_after=%s error=%s",
                           target_date, log_id, attempt, self.llm_retry_limit,
                           retry_after, reason)
            return
        def give_up(item: DailySnapshot) -> None:
            state = item.log_evaluations[log_id]
            state.positive_ai = state.positive_final = FAILED_EVALUATION_TEXT
            state.improvement_ai = state.improvement_final = FAILED_EVALUATION_TEXT
            state.status = (UnitStatus.WAITING_CONFIRMATION
                if state.evaluator_id else UnitStatus.CONFIRMED)
            state.evaluated_at = review_day_datetime(target_date)
            state.ai_evaluated_at = state.evaluated_at
            if state.status == UnitStatus.CONFIRMED:
                state.confirmed_at = state.evaluated_at
            state.retry_after = None
            state.error = reason
            state.fallback = "failed"
        self.store.update_snapshot(target_date, give_up)
        self._record_issue(
            target_date, f"log:{log_id}:ai-failed",
            f"日志 {log_id} 的 AI 评价重试 {self.llm_retry_limit} 次仍失败，已标为"
            f"「{FAILED_EVALUATION_TEXT}」并发布到评价表，请人工补评：{reason}")
        # ERROR + 堆栈才会走管理员告警；这条是整轮重试之后才发的唯一一次。
        logger.error("workflow1_log_evaluation_gave_up date=%s log_id=%s attempts=%d error=%s",
                     target_date, log_id, self.llm_retry_limit, reason,
                     exc_info=(type(exc), exc, exc.__traceback__))

    def retry_incomplete(self, *, final: bool = False) -> list[dict]:
        """重试没跑完的当天，只补没做完的部分。

        final=True 是当天最后一轮：自动处理仍未完成则记人工排查台账。
        已完成自动处理、尚未到截止时间的人工审核等待不属于失败。
        """
        today = utc_now().astimezone(ZoneInfo("Asia/Shanghai")).date()
        results: list[dict] = []
        for run in self.store.list_incomplete_workflows():
            if run.target_date < today - timedelta(days=INCOMPLETE_RETRY_WINDOW_DAYS):
                continue
            snapshot = self.store.load_snapshot(run.target_date)
            if snapshot is not None and MANUAL_REVIEW_KEY in snapshot.issues:
                continue
            result = self.start(run.target_date)
            results.append({"target_date": run.target_date.isoformat(),
                            "status": result.status, "last_error": result.last_error})
            if not final or result.status == WorkflowStatus.COMPLETED:
                continue
            snapshot = self.store.load_snapshot(run.target_date)
            if (result.status == WorkflowStatus.WAITING_EVALUATIONS
                    and not result.last_error
                    and snapshot is not None and snapshot.evaluations_published
                    and any(state.status == UnitStatus.WAITING_CONFIRMATION
                            for state in snapshot.log_evaluations.values())
                    and all(state.status in {UnitStatus.WAITING_CONFIRMATION, UnitStatus.CONFIRMED}
                            and not state.error and state.fallback != "failed"
                            for state in snapshot.log_evaluations.values())
                    and not self._deadline_passed(run.target_date)):
                logger.info("workflow1_waiting_manual_confirmation date=%s", run.target_date)
                continue
            reason = (f"本轮自动补跑结束，仍有处理未完成（当前状态 {result.status}）："
                      f"{result.last_error or '请检查评价处理、写回及报告生成状态'}；需人工排查")
            if snapshot is None:
                # 连快照都没有，台账写不进去，至少让管理员收到一条。
                logger.error("workflow1_needs_manual_review date=%s status=%s error=%s",
                             run.target_date, result.status, result.last_error or "-")
                continue
            self._record_issue(run.target_date, MANUAL_REVIEW_KEY, reason)
        return results

    def retry_failed_evaluations(self) -> list[str]:
        """到点重跑接口/网络失败的日志评价；被推迟的和次数用尽的不动。"""
        retried: list[str] = []
        now = utc_now()
        for run in self.store.list_incomplete_workflows():
            target_date = run.target_date
            snapshot = self.store.load_snapshot(target_date)
            if snapshot is None:
                continue
            due = {state.log_id for state in snapshot.log_evaluations.values()
                   if state.status == UnitStatus.FAILED and state.retry_after
                   and state.retry_after <= now}
            if not due:
                continue
            def reopen(item: DailySnapshot) -> None:
                for state in item.log_evaluations.values():
                    if state.log_id in due:
                        state.status = UnitStatus.PENDING
                        state.retry_after = None
                        state.error = None
            self.store.update_snapshot(target_date, reopen)
            logger.info("workflow1_evaluation_retry date=%s logs=%s",
                        target_date, sorted(due))
            self.start(target_date)
            retried.append(target_date.isoformat())
        return retried

    @workflow_diagnostics("workflow1")
    def _notify_once(self, target_date: date, key: str,
                     open_id: str, message: str) -> None:
        if not open_id:
            raise ValueError(f"通知 {key} 缺少 OpenID")
        if self.store.notification_sent(target_date, key):
            record_outcome(key, "already_sent")
            return

        def send():
            if confirmed_unsent(target_date):
                self.store.release_notification(target_date, key)
            if not self.store.reserve_notification(target_date, key):
                return {"message_id": ""}
            try:
                with diagnostic_context(notification_key=key, recipient=open_id):
                    result = self.messages.send_text(open_id, message, idempotency_key=key)
            except FeishuApiError:
                self.store.release_notification(target_date, key)
                raise
            message_id = str(result.get("message_id", ""))
            if not message_id:
                raise ValueError(f"飞书通知 {key} 未返回 message_id")
            self.store.complete_notification(target_date, key, message_id)
            return result

        if self.notifications is None:
            send()
        else:
            run = self.store.load_workflow(target_date)
            self.notifications.deliver(
                key=key, target_date=target_date, created_at=run.started_at,
                send_date=target_date + timedelta(days=1), receive_id=open_id, send=send,
                message=message, workflow="workflow1",
                message_type="review" if ":FORM:" in key else "report")

    def _notify_evaluators(self, target_date: date) -> None:
        snapshot = self.store.load_snapshot(target_date)
        assert snapshot is not None
        people = snapshot.organization.person_map()
        for evaluator_id, progress in snapshot.evaluators.items():
            if not progress.log_ids:
                self.store.update_snapshot(target_date,
                    lambda item: setattr(item.evaluators[evaluator_id], "closed", True))
                continue
            names = sorted({people[snapshot.log_evaluations[log_id].person_id].name
                            for log_id in progress.log_ids})
            key = f"{target_date}:FORM:{evaluator_id}"
            evaluator = people[evaluator_id]
            form_url = self.review_form_urls.get(evaluator.role)
            if not form_url:
                raise ValueError(f"评价人「{evaluator.name}」的角色 {evaluator.role} 未配置审核表单")
            open_id = evaluator.open_id or ""
            if open_id:
                scope = "骨干" if evaluator.role == "部长" else "成员"
                self._send_business_notification(target_date, key, open_id,
                    f"{greeting(evaluator)}"
                    f"请评价以下{scope} {date_cn(target_date)} 的工作日志"
                    f"（共 {len(progress.log_ids)} 条）：\n{'、'.join(names)}"
                    f"\n问卷：{form_url}")
            else:
                self._record_issue(
                    target_date, f"person:{evaluator_id}:missing-open-id",
                    f"评价人「{people[evaluator_id].name}」缺少 OpenID，"
                    "问卷通知未发送，请补录")
                record_outcome(key, "failed", error="评价人缺少 OpenID")
            self.store.update_snapshot(target_date,
                lambda item: setattr(item.evaluators[evaluator_id], "notified", True))

    def notify_evaluators_if_due(self, target_date: date) -> None:
        with self._lock:
            hour, minute = map(int, self.notify_at.split(":"))
            notify_time = datetime.combine(target_date + timedelta(days=1),
                                           time(hour, minute), ZoneInfo("Asia/Shanghai"))
            if (datetime.now(ZoneInfo("Asia/Shanghai")) < notify_time or
                    self._deadline_passed(target_date)):
                return
            snapshot = self.store.load_snapshot(target_date)
            if snapshot is not None:
                self._notify_evaluators(target_date)

    def _reconcile_human(self, target_date: date) -> int:
        snapshot = self.store.load_snapshot(target_date)
        if snapshot is None:
            raise ValueError(f"{target_date} 缺少快照，不能核对人工评价；请先检查该日期的启动错误")
        if not snapshot.evaluations_published:
            return 0
        accepted = 0
        parsed_records = {}
        for record in self.human_evaluations.for_date(snapshot):
            parsed = self.human_evaluations.parse(snapshot, record)
            if parsed is None:
                continue
            key, evaluation = parsed
            previous = parsed_records.get(key)
            if previous is not None and previous != evaluation:
                raise ValueError(f"问卷结果评价编号重复且内容冲突：{key}")
            parsed_records[key] = evaluation
        for evaluation in parsed_records.values():
            log_id = evaluation.log_id
            state = snapshot.log_evaluations[log_id]
            if (state.source == "HUMAN" and state.positive_final == evaluation.positive
                    and state.improvement_final == evaluation.improvement):
                continue
            progress = snapshot.evaluators[evaluation.evaluator_id]
            if progress.closed:
                continue
            def save(item: DailySnapshot) -> None:
                state = item.log_evaluations[log_id]
                state.positive_final = evaluation.positive
                state.improvement_final = evaluation.improvement
                state.source = "HUMAN"
                state.status = UnitStatus.CONFIRMED
                state.evaluated_at = utc_now()
                state.confirmed_at = state.evaluated_at
            self.store.update_snapshot(target_date, save)
            snapshot.log_evaluations[log_id].source = "HUMAN"
            accepted += 1
        return accepted

    def _deadline_passed(self, target_date: date) -> bool:
        if not self.auto_advance_at:
            return False
        hour, minute = map(int, self.auto_advance_at.split(":"))
        deadline = datetime.combine(target_date + timedelta(days=1),
                                    time(hour, minute), ZoneInfo("Asia/Shanghai"))
        return datetime.now(ZoneInfo("Asia/Shanghai")) >= deadline

    def _close_remaining(self, target_date: date) -> int:
        current = self.store.load_snapshot(target_date)
        if current is None or not current.evaluations_published:
            raise ValueError("AI 评价尚未全部写回，不能按截止规则放行")
        count = 0
        def close(snapshot: DailySnapshot) -> None:
            nonlocal count
            for progress in snapshot.evaluators.values():
                for log_id in progress.log_ids:
                    state = snapshot.log_evaluations[log_id]
                    if state.status == UnitStatus.FAILED:
                        continue  # 失败项已在台账，标注由文档层呈现，不阻塞放行
                    if state.status not in {UnitStatus.WAITING_CONFIRMATION,
                                            UnitStatus.CONFIRMED}:
                        raise ValueError(f"日志 {log_id} 尚无有效 AI 评价")
                    if state.status != UnitStatus.CONFIRMED:
                        state.status = UnitStatus.CONFIRMED
                        state.confirmed_at = utc_now()
                        if state.source != "HUMAN":
                            state.manual_skipped = True
                        count += 1
                progress.closed = True
        self.store.update_snapshot(target_date, close)
        return count

    @workflow_diagnostics("workflow1")
    def finalize_pending_confirmations(self, target_date: date) -> dict:
        with self._lock:
            human = auto = 0
            stage = "check_snapshot"
            try:
                snapshot = self.store.load_snapshot(target_date)
                if snapshot is None:
                    reason = "missing_snapshot" if snapshot is None else "evaluations_not_ready"
                    run = self.store.load_workflow(target_date)
                    logger.error("workflow1_finalize_not_ready date=%s reason=%s previous_error=%s",
                                 target_date, reason, run.last_error if run else None)
                    return {"target_date": target_date.isoformat(), "human_confirmed": 0,
                            "auto_confirmed": 0, "errors": [{"error": reason}], "status": "not_ready"}
                if self.auto_advance_at and not self._deadline_passed(target_date):
                    return {"target_date": str(target_date), "human_confirmed": 0,
                            "auto_confirmed": 0, "errors": [], "status": "waiting"}
                # Snapshot is frozen by the first successful close. Late human
                # events and late model responses cannot change the report.
                if not all(progress.closed for progress in snapshot.evaluators.values()):
                    human = self._reconcile_human(target_date)
                def fill_errors(item):
                    for state in item.log_evaluations.values():
                        if state.status in {UnitStatus.PENDING, UnitStatus.RUNNING, UnitStatus.FAILED}:
                            state.status = UnitStatus.WAITING_CONFIRMATION
                            state.positive_ai = state.positive_final = "生成错误：" + (state.error or "截止前未生成评价")
                            state.improvement_ai = state.improvement_final = "生成错误：评价缺失"
                            state.fallback = "failed"
                            state.retry_after = None
                    item.evaluations_published = True
                self.store.update_snapshot(target_date, fill_errors)
                stage = "close_remaining"
                auto = self._close_remaining(target_date)
                stage = "advance_documents_reports_notifications"
                self.advance(target_date)
                logger.info("workflow1_finalized date=%s human_confirmed=%d auto_confirmed=%d",
                            target_date, human, auto)
                return {"target_date": target_date.isoformat(),
                        "human_confirmed": human, "auto_confirmed": auto,
                        "errors": []}
            except Exception as exc:
                logger.exception("workflow1_finalize_failed date=%s stage=%s error=%s",
                                 target_date, stage, error_summary(exc))
                self.store.set_status(target_date, WorkflowStatus.FAILED,
                                      error=f"stage={stage} {error_summary(exc)}")
                return {"target_date": target_date.isoformat(),
                        "human_confirmed": human, "auto_confirmed": auto,
                        "errors": [{"error": error_summary(exc), "stage": stage}]}

    def handle_confirmation(self, request: ConfirmationRequest) -> dict:
        if request.table_name not in self.human_evaluations.tables:
            raise ValueError("Workflow1 仅处理人工评价表事件")
        if request.table_id != self.human_evaluations.tables[request.table_name].table_id:
            raise ValueError("人工评价表 ID 不匹配")
        target_date = date.fromisoformat(request.business_key.split(":", 1)[0])
        return self.handle_human_record(request.record_id, target_date=target_date,
                                        expected_key=request.business_key,
                                        table_id=request.table_id)

    def handle_human_record(self, record_id: str, *, target_date: date | None = None,
                            expected_key: str | None = None,
                            table_id: str | None = None) -> dict:
        with self._lock:
            record = (self.human_evaluations.load_record(record_id, table_id=table_id)
                      if table_id else self.human_evaluations.load_record(record_id))
            dates = ([target_date] if target_date else
                [run.target_date for run in self.store.list_incomplete_workflows()])
            for one_date in dates:
                snapshot = self.store.load_snapshot(one_date)
                if snapshot is None or not snapshot.evaluations_published:
                    continue
                parsed = self.human_evaluations.parse(snapshot, record)
                if parsed is None:
                    continue
                if expected_key and parsed[0] != expected_key:
                    raise ValueError("问卷事件与评价编号不一致")
                accepted = self._reconcile_human(one_date)
                return {"status": "confirmed" if accepted else "duplicate",
                        "business_key": parsed[0]}
            return {"status": "pending", "record_id": record_id}

    def advance(self, target_date: date) -> None:
        snapshot = self.store.load_snapshot(target_date)
        if snapshot is None or not snapshot.evaluations_published:
            return
        if self.auto_advance_at and not self._deadline_passed(target_date):
            self.store.set_status(target_date, WorkflowStatus.WAITING_EVALUATIONS)
            return
        objects = self.documents.advance(
            target_date,
            on_issue=lambda key, reason: self._record_issue(target_date, key, reason))
        report_issues = self.reports.publish(snapshot, objects)
        for key, reason in report_issues.items():
            self._record_issue(target_date, key, reason)
        self.notify_documents_if_due(target_date, snapshot=snapshot, objects=objects)
        if f"{target_date}:TEAM" in objects:
            self.store.set_status(target_date, WorkflowStatus.COMPLETED)
        else:
            self.store.set_status(target_date, WorkflowStatus.WAITING_EVALUATIONS)

    def _send_business_notification(self, target_date, key, open_id, message):
        try:
            self._notify_once(target_date, key, open_id, message)
        except Exception as exc:
            logger.exception("workflow1_notification_failed date=%s key=%s to=%s",
                             target_date, key, open_id)
            if manual_delivery(target_date):
                record_outcome(key, "failed", error=error_summary(exc))

    def _notify_documents(self, target_date, snapshot, objects):
        people = snapshot.organization.person_map()
        for department in snapshot.organization.departments:
            key = f"{target_date}:DEPARTMENT:{department.department_id}"
            if key in objects and department.minister_id:
                minister = people[department.minister_id]
                if minister.open_id:
                    department_name = department.name.removesuffix("部门")
                    self._send_business_notification(target_date, f"{target_date}:DOC:{key}",
                        minister.open_id,
                        f"{greeting(minister)}{date_cn(target_date)} 的"
                        f"{department_name}部门日志已生成：{objects[key].url}")
                else:
                    self._record_issue(
                        target_date, f"person:{minister.person_id}:missing-open-id",
                        f"部长「{minister.name}」缺少 OpenID，部门日志通知未发送")
                    record_outcome(f"{target_date}:DOC:{key}", "failed", error="部长缺少 OpenID")
        team_key = f"{target_date}:TEAM"
        if team_key in objects:
            if not snapshot.organization.team_leader_id:
                raise ValueError("团队负责人缺失")
            leader = people[snapshot.organization.team_leader_id]
            if leader.open_id:
                self._send_business_notification(target_date, f"{target_date}:DOC:{team_key}",
                    leader.open_id,
                    f"{greeting(leader)}{date_cn(target_date)} 的团队日志已生成："
                    f"{objects[team_key].url}")
            else:
                self._record_issue(
                    target_date, f"person:{leader.person_id}:missing-open-id",
                    f"团队负责人「{leader.name}」缺少 OpenID，团队日志通知未发送")
                record_outcome(f"{target_date}:DOC:{team_key}", "failed", error="团队负责人缺少 OpenID")

    def notify_documents_if_due(self, target_date: date, *, snapshot=None,
                                objects=None) -> None:
        """Send generated reports at 22:00 on the evaluation day, or once ready later."""
        hour, minute = map(int, self.report_notify_at.split(":"))
        notify_time = datetime.combine(target_date + timedelta(days=1),
                                       time(hour, minute), ZoneInfo("Asia/Shanghai"))
        if datetime.now(ZoneInfo("Asia/Shanghai")) < notify_time:
            return
        snapshot = snapshot or self.store.load_snapshot(target_date)
        if snapshot is None:
            return
        if objects is None:
            objects = {key: item for key, item in snapshot.cloud_objects.items()
                       if item.content_written and
                       (":DEPARTMENT:" in key or key == f"{target_date}:TEAM")}
        self._notify_documents(target_date, snapshot, objects)

    @workflow_diagnostics("workflow1")
    def send_notifications(self, target_date: date, message_type: str = "all",
                           confirm_unsent: bool = False) -> dict:
        with self._lock, manual_notifications(target_date, confirm_unsent) as results:
            if self.store.load_workflow(target_date) is None:
                raise LookupError(f"{target_date} 没有当日状态，可尝试从发送台账恢复")
            snapshot = self.store.load_snapshot(target_date)
            if snapshot is None:
                raise LookupError(f"{target_date} 没有当日快照")
            if message_type in {"all", "review"}:
                if snapshot.evaluations_published:
                    self._notify_evaluators(target_date)
                else:
                    record_outcome("review", "not_ready", reason="evaluations_not_ready")
            if message_type in {"all", "report"}:
                objects = {key: item for key, item in snapshot.cloud_objects.items()
                           if item.content_written and
                           (":DEPARTMENT:" in key or key == f"{target_date}:TEAM")}
                self._notify_documents(target_date, snapshot, objects)
                if not objects:
                    record_outcome("report", "not_ready", reason="documents_not_ready")
            # The policy and wrapper may observe the same error; return one outcome per key.
            unique = {item["key"]: item for item in results}
            return {"workflow": "workflow1", "target_date": target_date.isoformat(),
                    "messages": list(unique.values())}


DailyWorkflow = Workflow1

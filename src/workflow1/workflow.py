"""Daily Workflow1 policy. Feishu and DeepSeek transports live outside src."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from data import FileStateStore, utc_now
from data.repositories import LogRepository, OrganizationCache
from workflow1.snapshot import SnapshotBuilder
from llm import PromptService
from schema import WorkflowRun
from workflow1.models import ConfirmationRequest, DailySnapshot, LogPromptInput, LogPromptOutput, UnitStatus, WorkflowStatus
from tool.feishu import MessageService
from tool.errors import FeishuApiError
from workflow1.documents import DailyDocuments
from workflow1.evaluations import AiEvaluationRepository, HumanEvaluationRepository
from workflow1.reports import WorkflowReportRepository

logger = logging.getLogger(__name__)

# 通知称呼：部长/团队负责人称「老师」，骨干称「同学」；其余角色不加后缀。
ROLE_TITLES = {"部长": "老师", "团队负责人": "老师", "骨干学生": "同学"}


def date_cn(target_date: date) -> str:
    return f"{target_date.month}月{target_date.day}日"


def greeting(person) -> str:
    return f"{person.name}{ROLE_TITLES.get(person.role, '')}，"


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
                 llm_concurrency: int = 3,
                 auto_advance_at: str = "12:00",
                 notify_at: str = "08:00",
                 minister_review_form_url: str = "https://jwxnd3ayslt.feishu.cn/share/base/form/shrcnaBl2hPcNAs4nNBYI2STpGh",
                 backbone_review_form_url: str = "https://jwxnd3ayslt.feishu.cn/share/base/form/shrcngOWTo1tmzxm4V9blwKA1Dd",
                 prompt_path: str = "prompts/S01.txt",
                 admin_open_id: str = "") -> None:
        self.store = store
        self.organization_cache = organization_cache
        self.log_repository = log_repository
        self.ai_evaluations = ai_evaluations
        self.human_evaluations = human_evaluations
        self.prompt_service = prompt_service
        self.messages = messages
        self.documents = documents
        self.reports = reports
        self.llm_concurrency = llm_concurrency
        self.auto_advance_at = auto_advance_at
        self.notify_at = notify_at
        self.review_form_urls = {
            "部长": minister_review_form_url,
            "骨干学生": backbone_review_form_url,
        }
        self.prompt_path = Path(prompt_path)
        self.admin_open_id = admin_open_id
        self._lock = threading.RLock()

    def _record_issue(self, target_date: date, key: str, reason: str) -> None:
        """Append to the daily ledger and notify the admin exactly once."""
        def mutate(snapshot: DailySnapshot) -> bool:
            if key in snapshot.issues:
                return False
            snapshot.issues[key] = reason
            return True

        added = self.store.update_snapshot(target_date, mutate)
        if added:
            logger.warning("workflow1_issue_recorded date=%s key=%s reason=%s",
                           target_date, key, reason)
        if not added or not self.admin_open_id:
            return
        try:
            self.messages.send_text(
                self.admin_open_id,
                f"【Workflow1 异常】{target_date}\n{reason}",
                idempotency_key=f"issue:{target_date}:{key}",
            )
        except Exception:
            logger.exception("admin_issue_notification_failed key=%s", key)

    def start(self, target_date: date) -> WorkflowRun:
        with self._lock:
            run = self.store.get_or_create_workflow(target_date)
            if run.status == WorkflowStatus.COMPLETED:
                logger.debug("workflow1_already_completed date=%s", target_date)
                return run
            logger.info("workflow1_start date=%s", target_date)
            try:
                snapshot = self.store.load_snapshot(target_date)
                if snapshot is not None and snapshot.schema_version != 2:
                    raise ValueError("旧版 Workflow1 快照不能按新流程续跑；请人工迁移该日期状态")
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
                self.store.set_status(target_date, WorkflowStatus.FAILED, error=str(exc))
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
        if not pending:
            return
        self.store.set_status(target_date, WorkflowStatus.LOGS_RUNNING)
        template = self.prompt_path.read_text(encoding="utf-8").strip()
        if not template:
            raise ValueError(f"Workflow1 prompt 为空：{self.prompt_path}")
        logs = {log.log_id: log for log in snapshot.logs}

        def process(log_id: str) -> tuple[str, str, str]:
            state = snapshot.log_evaluations[log_id]
            log = logs[log_id]
            output = self.prompt_service.execute(
                prompt_code="LOG", template=template,
                input_data=LogPromptInput(target_date=target_date,
                    person_id=state.person_id, log_id=log_id, log=log.content()),
                output_model=LogPromptOutput)
            return log_id, output.positive, output.improvement

        failures = []
        with ThreadPoolExecutor(max_workers=self.llm_concurrency) as pool:
            futures = {pool.submit(process, state.log_id): state.log_id
                       for state in pending}
            for future in as_completed(futures):
                log_id = futures[future]
                try:
                    _, positive, improvement = future.result()
                    def save(item: DailySnapshot) -> None:
                        state = item.log_evaluations[log_id]
                        state.positive_ai = state.positive_final = positive
                        state.improvement_ai = state.improvement_final = improvement
                        state.status = (UnitStatus.WAITING_CONFIRMATION
                            if state.evaluator_id else UnitStatus.CONFIRMED)
                        state.evaluated_at = utc_now()
                        state.ai_evaluated_at = state.evaluated_at
                        if state.status == UnitStatus.CONFIRMED:
                            state.confirmed_at = state.evaluated_at
                        state.error = None
                    self.store.update_snapshot(target_date, save)
                except Exception as exc:
                    failures.append(log_id)
                    def fail(item: DailySnapshot) -> None:
                        state = item.log_evaluations[log_id]
                        state.status = UnitStatus.FAILED
                        state.error = str(exc)
                    self.store.update_snapshot(target_date, fail)
                    self._record_issue(
                        target_date, f"log:{log_id}:ai-failed",
                        f"日志 {log_id} AI 评价生成失败（重试已耗尽）：{exc}；"
                        "已跳过，可调用重评接口补评")
        logger.info("workflow1_logs_evaluated date=%s pending=%d failed=%d",
                    target_date, len(pending), len(failures))

    def _notify_once(self, target_date: date, key: str,
                     open_id: str, message: str) -> None:
        if not open_id:
            raise ValueError(f"通知 {key} 缺少 OpenID")
        if not self.store.reserve_notification(target_date, key):
            return
        try:
            result = self.messages.send_text(
                open_id, message, idempotency_key=key)
        except FeishuApiError:
            self.store.release_notification(target_date, key)
            raise
        message_id = str(result.get("message_id", ""))
        if not message_id:
            raise ValueError(f"飞书通知 {key} 未返回 message_id")
        self.store.complete_notification(target_date, key, message_id)

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
                self._notify_once(target_date, key, open_id,
                    f"{greeting(evaluator)}"
                    f"请评价以下{scope} {date_cn(target_date)} 的工作日志"
                    f"（共 {len(progress.log_ids)} 条）：\n{'、'.join(names)}"
                    f"\n问卷：{form_url}")
            else:
                self._record_issue(
                    target_date, f"person:{evaluator_id}:missing-open-id",
                    f"评价人「{people[evaluator_id].name}」缺少 OpenID，"
                    "问卷通知未发送，请补录")
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
        assert snapshot is not None
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
            if snapshot.log_evaluations[log_id].source == "HUMAN":
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
        def close(item: DailySnapshot) -> None:
            for progress in item.evaluators.values():
                if all(item.log_evaluations[log_id].source == "HUMAN"
                       for log_id in progress.log_ids):
                    progress.closed = True
        self.store.update_snapshot(target_date, close)
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

    def finalize_pending_confirmations(self, target_date: date) -> dict:
        with self._lock:
            try:
                human = self._reconcile_human(target_date)
                auto = self._close_remaining(target_date)
                self.advance(target_date)
                logger.info("workflow1_finalized date=%s human_confirmed=%d auto_confirmed=%d",
                            target_date, human, auto)
                return {"target_date": target_date.isoformat(),
                        "human_confirmed": human, "auto_confirmed": auto,
                        "errors": []}
            except Exception as exc:
                logger.warning("workflow1_finalize_failed date=%s error=%s",
                               target_date, exc)
                self.store.set_status(target_date, WorkflowStatus.FAILED,
                                      error=str(exc))
                return {"target_date": target_date.isoformat(),
                        "human_confirmed": 0, "auto_confirmed": 0,
                        "errors": [{"error": str(exc)}]}

    def handle_confirmation(self, request: ConfirmationRequest) -> dict:
        if request.table_name != "human_evaluations":
            raise ValueError("Workflow1 仅处理人工评价表事件")
        if request.table_id != self.human_evaluations.table.table_id:
            raise ValueError("人工评价表 ID 不匹配")
        target_date = date.fromisoformat(request.business_key.split(":", 1)[0])
        return self.handle_human_record(request.record_id, target_date=target_date,
                                        expected_key=request.business_key)

    def handle_human_record(self, record_id: str, *, target_date: date | None = None,
                            expected_key: str | None = None) -> dict:
        with self._lock:
            record = self.human_evaluations.load_record(record_id)
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
                self.advance(one_date)
                return {"status": "confirmed" if accepted else "duplicate",
                        "business_key": parsed[0]}
            return {"status": "pending", "record_id": record_id}

    def advance(self, target_date: date) -> None:
        snapshot = self.store.load_snapshot(target_date)
        if snapshot is None or not snapshot.evaluations_published:
            return
        objects = self.documents.advance(
            target_date,
            on_issue=lambda key, reason: self._record_issue(target_date, key, reason))
        report_issues = self.reports.publish(snapshot, objects)
        detail_issues = self.reports.publish_details(snapshot, objects)
        if isinstance(detail_issues, dict):
            report_issues.update(detail_issues)
        for key, reason in report_issues.items():
            self._record_issue(target_date, key, reason)
        people = snapshot.organization.person_map()
        for department in snapshot.organization.departments:
            key = f"{target_date}:DEPARTMENT:{department.department_id}"
            if key in objects and department.minister_id:
                minister = people[department.minister_id]
                if minister.open_id:
                    self._notify_once(target_date, f"{target_date}:DOC:{key}",
                        minister.open_id,
                        f"{greeting(minister)}{date_cn(target_date)} 的"
                        f"{department.name}部门日志已生成：{objects[key].url}")
                else:
                    self._record_issue(
                        target_date, f"person:{minister.person_id}:missing-open-id",
                        f"部长「{minister.name}」缺少 OpenID，部门日志通知未发送")
        team_key = f"{target_date}:TEAM"
        if team_key in objects:
            if not snapshot.organization.team_leader_id:
                raise ValueError("团队负责人缺失")
            leader = people[snapshot.organization.team_leader_id]
            if leader.open_id:
                self._notify_once(target_date, f"{target_date}:DOC:{team_key}",
                    leader.open_id,
                    f"{greeting(leader)}{date_cn(target_date)} 的团队日志已生成："
                    f"{objects[team_key].url}")
            else:
                self._record_issue(
                    target_date, f"person:{leader.person_id}:missing-open-id",
                    f"团队负责人「{leader.name}」缺少 OpenID，团队日志通知未发送")
            self.store.set_status(target_date, WorkflowStatus.COMPLETED)
        else:
            self.store.set_status(target_date, WorkflowStatus.WAITING_EVALUATIONS)


DailyWorkflow = Workflow1

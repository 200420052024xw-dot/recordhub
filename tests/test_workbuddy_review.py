"""WorkBuddy 批改接口：待批改清单、逐条提交、覆盖语义、暂存与独立 token。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config.settings import AppSettings, DeepSeekSettings, FeishuSettings  # noqa: E402
from schema import TableConfig  # noqa: E402
from schema.base import TableDefinition  # noqa: E402
from schema.domain import Department, Organization, Person, WorkLog  # noqa: E402
from service.api import create_app  # noqa: E402
from service.runtime import Runtime, WorkflowBinding  # noqa: E402
from service.workbuddy_review.models import (  # noqa: E402
    CLOSED,
    EMPTY_EDITED,
    ITEM_NOT_FOUND,
    KIND_LOG,
    NOT_YOURS,
    OK,
    PendingRequest,
    ReviewSubmitRequest,
)
from service.workbuddy_review.service import ReviewService  # noqa: E402
from service.workbuddy_review.settings import ReviewSettings  # noqa: E402
from service.workbuddy_review.store import ReviewInbox  # noqa: E402
from workflow1.models import (  # noqa: E402
    DailySnapshot,
    EvaluatorProgress,
    LogEvaluationState,
    UnitStatus,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
TODAY = date(2026, 10, 14)
YESTERDAY = date(2026, 10, 13)
FIXED_NOW = datetime(2026, 10, 14, 10, 0, tzinfo=SHANGHAI)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make_organization() -> Organization:
    persons = [
        Person(person_id="L1", name="负责人甲", role="团队负责人"),
        Person(person_id="M1", name="部长甲", role="部长", department_id="D1", leader_id="L1"),
        Person(person_id="B1", name="骨干甲", role="骨干学生", department_id="D1", leader_id="M1"),
        Person(person_id="S1", name="基层甲", role="基层学生", department_id="D1", leader_id="B1"),
        Person(person_id="S2", name="基层乙", role="基层学生", department_id="D1", leader_id="B1"),
    ]
    departments = [Department(department_id="D1", name="研发部", minister_id="M1")]
    return Organization(persons=persons, departments=departments, team_leader_id="L1")


def make_log(log_id: str, person_id: str, *, progress: str = "进展") -> WorkLog:
    return WorkLog(log_id=log_id, person_id=person_id, submitted_at=FIXED_NOW,
                   progress=progress, difficulties="困难", reflection="反思", other="其他")


def awaiting(log_id: str, person_id: str, evaluator_id: str = "B1", *,
             positive: str = "肯定", improvement: str = "改进",
             fallback: str = "", evaluation_id: str = "") -> LogEvaluationState:
    return LogEvaluationState(
        log_id=log_id, person_id=person_id, evaluator_id=evaluator_id,
        status=UnitStatus.WAITING_CONFIRMATION, positive_ai=positive,
        improvement_ai=improvement, fallback=fallback, evaluation_id=evaluation_id)


def make_snapshot(day: date, organization: Organization, logs,
                  evaluations: dict) -> DailySnapshot:
    evaluators: dict[str, EvaluatorProgress] = {}
    for log_id, state in evaluations.items():
        evaluators.setdefault(state.evaluator_id, EvaluatorProgress(
            evaluator_id=state.evaluator_id)) \
            .log_ids.append(log_id)
    return DailySnapshot(
        workflow_run_id=f"run:{day.isoformat()}", target_date=day,
        organization=organization, logs=list(logs), submission_status={},
        log_evaluations=evaluations, evaluators=evaluators,
        created_at=FIXED_NOW, updated_at=FIXED_NOW)


class FakeOrganizationCache:
    def __init__(self, organization): self.organization = organization
    def get(self): return self.organization


class FakeSnapshotStore:
    def __init__(self, snapshots=None): self.snapshots = snapshots or {}
    def load_snapshot(self, day): return self.snapshots.get(day)


class FakeBitable:
    def __init__(self, rows=None):
        self.rows = {key: list(value) for key, value in (rows or {}).items()}
        self.created: list[tuple[str, dict]] = []
        self.updated: list[tuple[str, str, dict]] = []

    def list_records(self, table_id, **kwargs): return list(self.rows.get(table_id, []))

    def create_record(self, table_id, fields):
        record = {"record_id": f"new{len(self.created)}", "fields": dict(fields)}
        self.created.append((table_id, fields))
        self.rows.setdefault(table_id, []).append(record)
        return record

    def update_record(self, table_id, record_id, fields):
        self.updated.append((table_id, record_id, fields))
        return {"record_id": record_id, "fields": fields}


def build_service(*, organization=None, snapshot=None, inbox=None, clock=None,
                  bitable=None, tables=None, page_size=50) -> ReviewService:
    organization = organization or make_organization()
    snapshots = {YESTERDAY: snapshot} if snapshot is not None else {}
    return ReviewService(
        organization_cache=FakeOrganizationCache(organization),
        snapshot_store=FakeSnapshotStore(snapshots),
        inbox=inbox or ReviewInbox(tempfile.mkdtemp()),
        settings=ReviewSettings(closes_at="21:40"),
        cursor_secret="review-secret",
        bitable=bitable,
        tables=tables,
        clock=clock or (lambda: FIXED_NOW),
        page_size=page_size,
    )


def log_snapshot(organization: Organization, logs, evaluations) -> DailySnapshot:
    return make_snapshot(YESTERDAY, organization, logs, evaluations)


class ReviewInboxTests(unittest.TestCase):
    def test_record_overwrite_and_reload(self) -> None:
        inbox = ReviewInbox(tempfile.mkdtemp())
        inbox.record("log", "骨干甲", reviewer_id="B1", target_date=TODAY,
                     item_id="log1", payload={"decision": "accept", "received_at": "t1"})
        inbox.record("log", "骨干甲", reviewer_id="B1", target_date=TODAY,
                     item_id="log2", payload={"decision": "edit", "received_at": "t2"})
        # 同一条再提交：后覆盖前。
        inbox.record("log", "骨干甲", reviewer_id="B1", target_date=TODAY,
                     item_id="log1", payload={"decision": "edit", "received_at": "t3"})
        document = inbox.load("log", "骨干甲")
        self.assertEqual(document["target_date"], TODAY.isoformat())
        self.assertEqual(set(document["items"]), {"log1", "log2"})
        self.assertEqual(document["items"]["log1"]["received_at"], "t3")

    def test_cross_day_resets_items(self) -> None:
        inbox = ReviewInbox(tempfile.mkdtemp())
        inbox.record("log", "骨干甲", reviewer_id="B1", target_date=YESTERDAY,
                     item_id="old", payload={"decision": "accept", "received_at": "t1"})
        inbox.record("log", "骨干甲", reviewer_id="B1", target_date=TODAY,
                     item_id="new", payload={"decision": "accept", "received_at": "t2"})
        document = inbox.load("log", "骨干甲")
        self.assertEqual(document["target_date"], TODAY.isoformat())
        self.assertEqual(set(document["items"]), {"new"})

    def test_documents_iterates_every_reviewer(self) -> None:
        inbox = ReviewInbox(tempfile.mkdtemp())
        for name, pid in (("骨干甲", "B1"), ("部长甲", "M1")):
            inbox.record("log", name, reviewer_id=pid, target_date=TODAY,
                         item_id="log1", payload={"decision": "accept"})
        self.assertEqual({d["reviewer_name"] for d in inbox.documents("log")},
                         {"骨干甲", "部长甲"})

    def test_corrupt_file_raises(self) -> None:
        inbox = ReviewInbox(tempfile.mkdtemp())
        path = inbox.path("log", "骨干甲")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            inbox.load("log", "骨干甲")


class PendingTests(unittest.TestCase):
    def _service(self, *, evaluations, logs):
        organization = make_organization()
        snapshot = log_snapshot(organization, logs, evaluations)
        return build_service(organization=organization, snapshot=snapshot)

    def test_log_queue_carries_text_and_ai(self) -> None:
        logs = [make_log("log1", "S1", progress="写了接口"),
                make_log("log2", "S2")]
        service = self._service(
            logs=logs,
            evaluations={"log1": awaiting("log1", "S1"),
                         "log2": awaiting("log2", "S2")})
        result = service.pending(PendingRequest(person_id="B1"))
        self.assertEqual(result["code"], OK)
        self.assertEqual([item["item_id"] for item in result["items"]], ["log1", "log2"])
        first = result["items"][0]
        self.assertEqual(first["name"], "基层甲")
        self.assertEqual(first["log"]["progress"], "写了接口")
        self.assertEqual(first["positive"], "肯定")
        self.assertEqual(first["ai_status"], "ok")
        self.assertEqual(result["deadlines"][0]["deadline_at"],
                         "2026-10-14T21:40:00+08:00")

    def test_failed_item_enters_queue_without_ai_text(self) -> None:
        service = self._service(
            logs=[make_log("log1", "S1")],
            evaluations={"log1": awaiting("log1", "S1", fallback="failed")})
        item = service.pending(PendingRequest(person_id="B1"))["items"][0]
        self.assertEqual(item["ai_status"], "failed")
        self.assertNotIn("positive", item)
        self.assertNotIn("improvement", item)

    def test_missing_lists_reports_without_logs(self) -> None:
        # 只有 S1 交了日志，S2 没交。
        service = self._service(
            logs=[make_log("log1", "S1")],
            evaluations={"log1": awaiting("log1", "S1")})
        result = service.pending(PendingRequest(person_id="B1"))
        self.assertEqual(result["missing"], [{"date": "2026-10-13", "name": "基层乙"}])

    def test_other_reviewer_sees_nothing(self) -> None:
        service = self._service(
            logs=[make_log("log1", "S1")],
            evaluations={"log1": awaiting("log1", "S1")})
        # 部长不批基层的日志。
        result = service.pending(PendingRequest(person_id="M1"))
        self.assertEqual(result["items"], [])

    def test_closed_after_deadline(self) -> None:
        clock = Clock(datetime(2026, 10, 14, 21, 40, tzinfo=SHANGHAI))
        service = build_service(clock=clock)
        self.assertEqual(service.pending(PendingRequest(person_id="B1"))["code"], CLOSED)

    def test_unknown_person_is_not_yours(self) -> None:
        service = build_service()
        self.assertEqual(service.pending(PendingRequest(person_id="ghost"))["code"],
                         NOT_YOURS)


class SubmitTests(unittest.TestCase):
    def _service(self, *, clock=None):
        organization = make_organization()
        snapshot = log_snapshot(organization, [make_log("log1", "S1")],
                                {"log1": awaiting("log1", "S1")})
        return build_service(organization=organization, snapshot=snapshot, clock=clock)

    def test_accept_then_edit_overwrites(self) -> None:
        clock = Clock(FIXED_NOW)
        service = self._service(clock=clock)
        first = service.submit(ReviewSubmitRequest(
            person_id="B1", item_id="log1", decision="accept"))
        self.assertEqual(first["code"], OK)
        self.assertEqual(first["decision"], "accept")
        clock.now = datetime(2026, 10, 14, 11, 0, tzinfo=SHANGHAI)
        second = service.submit(ReviewSubmitRequest(
            person_id="B1", item_id="log1", decision="edit",
            edited={"positive": "改后的肯定", "improvement": "改后的改进"}))
        self.assertEqual(second["code"], OK)
        stored = service.inbox.load("log", "骨干甲")["items"]["log1"]
        self.assertEqual(stored["decision"], "edit")
        self.assertEqual(stored["edited"]["positive"], "改后的肯定")
        self.assertEqual(stored["received_at"], "2026-10-14T11:00:00+08:00")
        self.assertEqual(stored["ai_snapshot"], {"positive": "肯定", "improvement": "改进"})

    def test_edit_without_content_is_empty(self) -> None:
        service = self._service()
        result = service.submit(ReviewSubmitRequest(
            person_id="B1", item_id="log1", decision="edit", edited={"positive": "  "}))
        self.assertEqual(result["code"], EMPTY_EDITED)

    def test_unknown_item(self) -> None:
        service = self._service()
        result = service.submit(ReviewSubmitRequest(
            person_id="B1", item_id="nope", decision="accept"))
        self.assertEqual(result["code"], ITEM_NOT_FOUND)

    def test_not_yours(self) -> None:
        # 部长不批基层日志。
        service = self._service()
        result = service.submit(ReviewSubmitRequest(
            person_id="M1", item_id="log1", decision="accept"))
        self.assertEqual(result["code"], NOT_YOURS)

    def test_closed(self) -> None:
        clock = Clock(datetime(2026, 10, 14, 21, 41, tzinfo=SHANGHAI))
        service = self._service(clock=clock)
        result = service.submit(ReviewSubmitRequest(
            person_id="B1", item_id="log1", decision="accept"))
        self.assertEqual(result["code"], CLOSED)


REPORT_WHEN = "2026-10-13T00:00:00+08:00"


def report_tables() -> TableConfig:
    return TableConfig(tables={
        "stage_analysis": TableDefinition(table_id="tbl_sa", fields={
            "task_id": "任务编号", "minister_ref": "部长",
            "progress": "阶段工作进展", "analysis_date": "日期"}),
        "stage_confirmation": TableDefinition(table_id="tbl_sc", fields={
            "task_id": "任务编号", "analysis_date": "日期", "submitted_by": "填写人：",
            "edited": "阶段工作修改", "draft": "阶段工作总结",
            "decision": "阶段工作总结是否确认"}),
        "monthly_department_analysis": TableDefinition(table_id="tbl_ma", fields={
            "task_id": "任务编号", "minister_ref": "部长名称：", "analysis_date": "日期",
            "personnel_s05": "降级—奖励—退出—提拔名单",
            "ideas_s06": "idea 清单", "meeting_s07": "学生例会重点人员"}),
        "monthly_department_confirmation": TableDefinition(table_id="tbl_mc", fields={
            "task_id": "任务编号", "minister_ref": "部长名称：", "analysis_date": "日期",
            "personnel_draft": "d1", "personnel_decision": "降级—奖励—退出—提拔名单是否确认",
            "personnel_edited": "e1", "ideas_draft": "d2", "ideas_decision": "idea清单是否确认",
            "ideas_edited": "e2", "meeting_draft": "d3",
            "meeting_decision": "学生例会重点人员是否确认", "meeting_edited": "e3"}),
    })


def report_bitable(*, stage_confirmed: bool = False) -> FakeBitable:
    confirmation = ([{"record_id": "sc1", "fields": {
        "任务编号": "run1:D1", "阶段工作总结是否确认": "确认无误"}}]
        if stage_confirmed else [])
    return FakeBitable({
        "tbl_sa": [
            {"record_id": "sa0", "fields": {
                "任务编号": "run0:D1", "部长": "部长甲",
                "日期": "2026-10-10T00:00:00+08:00", "阶段工作进展": "上一轮总结"}},
            {"record_id": "sa1", "fields": {
                "任务编号": "run1:D1", "部长": "部长甲", "日期": REPORT_WHEN,
                "阶段工作进展": "阶段总结正文"}}],
        "tbl_sc": confirmation,
        "tbl_ma": [{"record_id": "ma1", "fields": {
            "任务编号": "run2:D1", "部长名称：": "部长甲", "日期": REPORT_WHEN,
            "降级—奖励—退出—提拔名单": "名单正文", "idea 清单": "idea正文",
            "学生例会重点人员": ""}}],
        "tbl_mc": [],
    })


class ReportQueueTests(unittest.TestCase):
    def _service(self, **kwargs) -> ReviewService:
        return build_service(bitable=report_bitable(**kwargs), tables=report_tables())

    def test_minister_sees_stage_draft(self) -> None:
        result = self._service().pending(PendingRequest(person_id="M1", kind="stage"))
        self.assertEqual(result["code"], OK)
        self.assertEqual([item["item_id"] for item in result["items"]], ["run1:D1"])
        item = result["items"][0]
        self.assertEqual(item["kind"], "stage_summary")
        self.assertEqual(item["name"], "研发部")
        self.assertEqual(item["sections"],
                         [{"label": "阶段工作进展", "text": "阶段总结正文"}])

    def test_minister_sees_monthly_draft_sections(self) -> None:
        result = self._service().pending(PendingRequest(person_id="M1", kind="monthly"))
        item = result["items"][0]
        self.assertEqual([section["label"] for section in item["sections"]],
                         ["降级—奖励—退出—提拔名单", "idea 清单"])

    def test_report_queue_keeps_only_latest_round(self) -> None:
        # 同部门攒了两轮未确认草稿，只出最近一轮。
        result = self._service().pending(PendingRequest(person_id="M1", kind="stage"))
        self.assertEqual([item["item_id"] for item in result["items"]], ["run1:D1"])
        self.assertEqual(result["items"][0]["sections"][0]["text"], "阶段总结正文")

    def test_confirmed_draft_is_hidden(self) -> None:
        result = self._service(stage_confirmed=True).pending(
            PendingRequest(person_id="M1", kind="stage"))
        self.assertEqual(result["items"], [])

    def test_non_minister_sees_nothing(self) -> None:
        result = self._service().pending(PendingRequest(person_id="B1", kind="stage"))
        self.assertEqual(result["items"], [])

    def test_submit_report_stores_sections(self) -> None:
        service = self._service()
        result = service.submit(ReviewSubmitRequest(
            person_id="M1", item_id="run1:D1", decision="edit",
            edited={"sections": [{"label": "阶段工作总结", "text": "改后的正文"}]}))
        self.assertEqual(result["code"], OK)
        stored = service.inbox.load("stage", "部长甲")["items"]["run1:D1"]
        self.assertEqual(stored["edited"]["sections"],
                         [{"label": "阶段工作总结", "text": "改后的正文"}])
        self.assertEqual(stored["ai_snapshot"]["sections"],
                         [{"label": "阶段工作进展", "text": "阶段总结正文"}])


def evaluation_tables() -> TableConfig:
    shared = {
        "evaluation_id": "评价编号", "source_log": "日志原文", "evaluated_at": "评价时间",
        "positive_final": "肯定之处_人工", "improvement_final": "改进之处_人工",
        "submitted_by": "填写人", "positive_confirmed": "肯定之处_AI是否确认",
        "improvement_confirmed": "需改进之处_AI是否确认："}
    return TableConfig(tables={
        "human_evaluations": TableDefinition(
            table_id="tbl_he", fields={**shared, "backbone_ref": "被审核骨干："}),
        "human_evaluations_backbone": TableDefinition(
            table_id="tbl_heb", fields={**shared, "basic_name": "被审核基层："}),
    })


class MergeTests(unittest.TestCase):
    def _service(self, *, bitable, tables=None, snapshot=None) -> ReviewService:
        organization = make_organization()
        snapshot = snapshot if snapshot is not None else log_snapshot(
            organization, [make_log("log1", "S1")],
            {"log1": awaiting("log1", "S1", evaluation_id="2026-10-13:EVAL:B1:log1")})
        return build_service(organization=organization, snapshot=snapshot,
                             bitable=bitable, tables=tables or evaluation_tables())

    def test_merge_writes_log_row_and_clears_stash(self) -> None:
        bitable = FakeBitable()
        service = self._service(bitable=bitable)
        service.submit(ReviewSubmitRequest(
            person_id="B1", item_id="log1", decision="edit",
            edited={"positive": "改肯定", "improvement": "改改进"}))
        written = service.merge(YESTERDAY)
        self.assertEqual(written["logs"], 1)
        table_id, fields = bitable.created[0]
        self.assertEqual(table_id, "tbl_heb")
        self.assertEqual(fields["被审核基层："], "基层甲")
        self.assertEqual(fields["肯定之处_人工"], "改肯定")
        self.assertEqual(fields["肯定之处_AI是否确认"], "需修改")
        # 自动编号 / 查找引用列交给飞书，不写。
        self.assertNotIn("评价编号", fields)
        self.assertNotIn("日志原文", fields)
        self.assertIsNone(service.inbox.load("log", "骨干甲"))

    def test_merge_is_idempotent_same_name_and_day(self) -> None:
        bitable = FakeBitable()
        service = self._service(bitable=bitable)
        service.submit(ReviewSubmitRequest(person_id="B1", item_id="log1", decision="accept"))
        service.merge(YESTERDAY)
        self.assertEqual(len(bitable.created), 1)
        # 同一天同一人再合并一次：认到已有的行，不再新建。
        service.submit(ReviewSubmitRequest(person_id="B1", item_id="log1", decision="accept"))
        service.merge(YESTERDAY)
        self.assertEqual(len(bitable.created), 1)

    def test_accept_uses_ai_text(self) -> None:
        bitable = FakeBitable()
        service = self._service(bitable=bitable)
        service.submit(ReviewSubmitRequest(person_id="B1", item_id="log1", decision="accept"))
        service.merge(YESTERDAY)
        _, fields = bitable.created[0]
        self.assertEqual(fields["肯定之处_人工"], "肯定")
        self.assertEqual(fields["肯定之处_AI是否确认"], "确认无误")

    def test_later_feishu_row_is_kept(self) -> None:
        existing = {"record_id": "he1", "created_time": "2026-10-14T12:00:00+08:00",
                    "fields": {"被审核基层：": "基层甲",
                               "评价时间": "2026-10-14T00:00:00+08:00",
                               "肯定之处_人工": "飞书里的"}}
        bitable = FakeBitable({"tbl_heb": [existing]})
        service = self._service(bitable=bitable)
        service.submit(ReviewSubmitRequest(person_id="B1", item_id="log1", decision="accept"))
        written = service.merge(YESTERDAY)
        self.assertEqual(written["logs"], 0)
        self.assertEqual(bitable.updated, [])

    def test_feishu_row_after_cutoff_is_overwritten(self) -> None:
        # 接口截止（21:40）之后才提交的表单行忽略：不让它压过对话里的批改。
        late = {"record_id": "he1", "created_time": "2026-10-14T21:45:00+08:00",
                "fields": {"被审核基层：": "基层甲",
                           "评价时间": "2026-10-14T00:00:00+08:00",
                           "肯定之处_人工": "表单里迟到的"}}
        bitable = FakeBitable({"tbl_heb": [late]})
        service = self._service(bitable=bitable)
        service.submit(ReviewSubmitRequest(person_id="B1", item_id="log1", decision="accept"))
        written = service.merge(YESTERDAY)
        self.assertEqual(written["logs"], 1)
        self.assertEqual(bitable.created, [])
        self.assertEqual(len(bitable.updated), 1)
        self.assertEqual(bitable.updated[0][1], "he1")

    def test_merge_writes_stage_confirmation_row(self) -> None:
        bitable = report_bitable()
        service = build_service(bitable=bitable, tables=report_tables())
        service.submit(ReviewSubmitRequest(
            person_id="M1", item_id="run1:D1", decision="edit",
            edited={"sections": [{"label": "阶段工作进展", "text": "改后的正文"}]}))
        written = service.merge(YESTERDAY)
        self.assertEqual(written["reports"], 1)
        table_id, fields = bitable.created[0]
        self.assertEqual(table_id, "tbl_sc")
        self.assertEqual(fields["任务编号"], "run1:D1")
        self.assertEqual(fields["阶段工作总结是否确认"], "需修改")
        self.assertEqual(fields["阶段工作修改"], "改后的正文")


class SchedulerWiringTests(unittest.TestCase):
    def _plan(self, review) -> list[dict]:
        from types import SimpleNamespace
        from config.schedules import load_schedule_config
        from service.orchestration import Orchestrator
        binding = SimpleNamespace(workflow=Mock(), auto_advance_at="21:50")
        runtime = SimpleNamespace(workflows={"workflow1_daily": binding}, workflow2=None,
                                  prompt_repository=None, review=review)
        settings = SimpleNamespace(state_dir=tempfile.mkdtemp(), llm_concurrency=3,
            llm_retry_interval_minutes=60, llm_retry_limit=3, workflow2_enabled=False)
        orchestration = Orchestrator(runtime, load_schedule_config(), Mock(), settings)
        self.addCleanup(orchestration.tasks.close)
        orchestration.plan_day(date(2026, 10, 14))
        return [t for t in orchestration.tasks.journal.all() if t["action"] == "review_merge"]

    def test_plan_day_books_merge_at_interface_cutoff(self) -> None:
        tasks = self._plan(Mock())
        self.assertEqual(len(tasks), 1)
        self.assertTrue(tasks[0]["due"].startswith("2026-10-14T21:40"))
        # 参数是日志日（审核日的前一天）
        self.assertEqual(tasks[0]["payload"]["day"], "2026-10-13")

    def test_missing_review_service_books_nothing(self) -> None:
        self.assertEqual(self._plan(None), [])


class TokenSeparationTests(unittest.TestCase):
    def _settings(self, review_token: str = "review-token") -> AppSettings:
        return AppSettings(
            feishu=FeishuSettings("app", "secret", "base"),
            deepseek=DeepSeekSettings("key"),
            admin_token="admin-token", workbuddy_token="workbuddy-token",
            query_token="query-token", review_token=review_token)

    def _client(self, settings: AppSettings):
        from fastapi.testclient import TestClient
        binding = WorkflowBinding(name="workflow1_daily", workflow=Mock(), store=Mock(),
                                  auto_advance_at="", confirmation_webhook_token="token",
                                  record_handlers={})
        service = Mock()
        service.pending.return_value = {"code": OK, "items": []}
        service.submit.return_value = {"code": OK, "item_id": "log1"}
        service.merge.return_value = {"logs": 0, "reports": 0}
        runtime = Runtime(settings=settings, infrastructure=Mock(),
                          organization_cache=Mock(),
                          workflows={binding.name: binding}, review=service)
        return TestClient(create_app(settings, runtime))

    def test_review_endpoints_require_the_review_token(self) -> None:
        client = self._client(self._settings())
        body = {"person_id": "B1", "kind": "log"}
        for headers in ({}, {"Authorization": "Bearer admin-token"},
                        {"Authorization": "Bearer query-token"}):
            with self.subTest(headers=headers):
                self.assertEqual(
                    client.post("/workbuddy/v1/reviews/pending", json=body, headers=headers)
                    .status_code, 401)
        ok = client.post("/workbuddy/v1/reviews/pending", json=body,
                         headers={"Authorization": "Bearer review-token"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["code"], OK)

    def test_review_token_cannot_reach_admin(self) -> None:
        client = self._client(self._settings())
        response = client.get("/admin/organization/persons",
                              headers={"Authorization": "Bearer review-token"})
        self.assertEqual(response.status_code, 401)

    def test_admin_merge_needs_admin_token(self) -> None:
        client = self._client(self._settings())
        denied = client.post("/admin/reviews/merge/2026-10-13",
                             headers={"Authorization": "Bearer review-token"})
        self.assertEqual(denied.status_code, 401)
        ok = client.post("/admin/reviews/merge/2026-10-13",
                         headers={"Authorization": "Bearer admin-token"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json(), {"logs": 0, "reports": 0})

    def test_missing_review_token_configures_503(self) -> None:
        client = self._client(self._settings(review_token=""))
        response = client.post("/workbuddy/v1/reviews/pending",
                               json={"person_id": "B1", "kind": "log"},
                               headers={"Authorization": "Bearer anything"})
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()

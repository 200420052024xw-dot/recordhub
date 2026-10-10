"""对话入口提交日志:身份、提交窗口、只增缓存、次日写回。"""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config import AppSettings, DeepSeekSettings, FeishuSettings
from data.store import FileStateStore
from schema import Department, Organization, Person, TableConfig
from service.api import create_app
from service.runtime import Runtime, WorkflowBinding
from tool.bitable_fields import SHANGHAI
from workflow1.models import DailySnapshot, WorkBuddySubmitRequest
from workflow1.settings import LogSubmitSettings
from workflow1.workbuddy_inbox import (
    CLOSED,
    EMPTY_CONTENT,
    NO_PERSON,
    OK,
    PERSON_ID_UNKNOWN,
    ROLE_NOT_SUPPORTED,
    LogSubmitService,
    WorkBuddyInbox,
)

DAY = date(2026, 10, 10)


class FrozenDatetime(datetime):
    """把 datetime.now() 钉在某个时刻,窗口判定才有确定的断言。"""

    frozen = datetime(2026, 10, 10, 20, 0, tzinfo=SHANGHAI)

    @classmethod
    def now(cls, tz=None):  # noqa: D102
        return cls.frozen.astimezone(tz) if tz else cls.frozen.replace(tzinfo=None)


class Bitable:
    def __init__(self, rows=None):
        self.rows = rows or {}
        self.created = []

    def iter_records(self, table_id, **kwargs):
        return list(self.rows.get(table_id, []))

    def batch_create(self, table_id, records):
        rows = list(records)
        self.created.extend(rows)
        return [{"record_id": f"rec{index}", "fields": row}
                for index, row in enumerate(rows, 1)]


class ExternalRecords:
    """external_records 的最小替身:业务键 → record_id。"""

    def __init__(self):
        self.mapped = {}

    def get_external_record(self, target_date, business_key):
        return self.mapped.get((target_date, business_key))

    def map_external_record(self, target_date, business_key, table_name, record_id):
        existing = self.mapped.get((target_date, business_key))
        if existing and existing["record_id"] != record_id:
            raise ValueError("Business key already maps to another record")
        self.mapped[(target_date, business_key)] = {
            "table_name": table_name, "record_id": record_id}


def table_config() -> TableConfig:
    return TableConfig.model_validate({"tables": {"logs": {
        "table_id": "logs", "fields": {
            "log_id": "自动编号", "submitted_at": "提交时间",
            "submitter_name": "姓名：", "submitter_ref": "提交人",
            "progress": "工作进展：", "difficulties": "工作困难：",
            "reflection": "心得反思：", "other": "其他：",
            "full_log": "完整日志"}}}})


class WorkBuddyInboxTests(unittest.TestCase):
    def setUp(self):
        self.organization = Organization(persons=[
            Person(person_id="B", name="骨干甲", role="骨干学生", open_id="ou_b",
                   department_id="D", leader_id="T"),
            Person(person_id="S001", name="基层乙", role="基层学生", open_id=None,
                   department_id="D", leader_id="B"),
            Person(person_id="M1", name="部长丙", role="部长", open_id="ou_m",
                   department_id="D"),
        ], departments=[Department(department_id="D", name="一部", minister_id="M1")])
        self.bitable = Bitable()
        self.workflow_store = ExternalRecords()
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LogSubmitService(
            inbox=WorkBuddyInbox(self.temp.name),
            organization_cache=SimpleNamespace(get=lambda: self.organization),
            bitable=self.bitable,
            table_config=table_config(),
            workflow_store=self.workflow_store,
            settings=LogSubmitSettings(
                submit_opens_at="12:00", submit_closes_at="23:50",
                fallback_form_url="https://example.test/form",
                archive_at="01:00"),
        )
        self.frozen = patch("workflow1.workbuddy_inbox.datetime", FrozenDatetime)
        self.frozen.start()
        self.addCleanup(self.frozen.stop)

    def freeze(self, hour, minute=0):
        FrozenDatetime.frozen = datetime(2026, 10, 10, hour, minute, tzinfo=SHANGHAI)

    # ------------------------------------------------------------- 认人

    def test_unique_name_returns_department_and_window(self):
        result = self.service.resolve("  基层乙  ")
        self.assertEqual(result["code"], OK)
        self.assertEqual(result["person_id"], "S001")
        self.assertEqual(result["department_name"], "一部")
        self.assertEqual(result["submit_window"]["state"], "open")
        self.assertEqual(result["fallback_form_url"], "https://example.test/form")

    def test_unknown_name_is_not_guessed(self):
        self.assertEqual(self.service.resolve("查无此人")["code"], NO_PERSON)

    def test_duplicate_name_is_refused_not_guessed(self):
        self.organization.persons.append(
            Person(person_id="S099", name="基层乙", role="基层学生",
                   department_id="D"))
        self.assertEqual(self.service.resolve("基层乙")["code"], NO_PERSON)

    def test_minister_role_is_out_of_scope(self):
        self.assertEqual(self.service.resolve("部长丙")["code"], ROLE_NOT_SUPPORTED)

    # --------------------------------------------------------- 提交窗口

    def test_window_open_during_the_day(self):
        self.freeze(13)
        self.assertEqual(self.service.submit_window()["state"], "open")

    def test_window_closed_after_stop_and_reopens_next_noon(self):
        self.freeze(23, 55)
        window = self.service.submit_window()
        self.assertEqual(window["state"], "closed")
        self.assertEqual(window["next_open_at"],
                         "2026-10-11T12:00:00+08:00")

    def test_window_closed_before_noon_reopens_same_day(self):
        self.freeze(9)
        window = self.service.submit_window()
        self.assertEqual(window["state"], "closed")
        self.assertEqual(window["next_open_at"],
                         "2026-10-10T12:00:00+08:00")

    # ----------------------------------------------------------- 提交

    def test_submit_writes_one_cache_file_and_no_feishu_row(self):
        self.freeze(20)
        result = self.service.submit(WorkBuddySubmitRequest(
            name="骨干甲", progress="完成联调", reflection="学到不少"))
        self.assertEqual(result["code"], OK)
        self.assertEqual(result["log_date"], "2026-10-10")
        self.assertEqual(result["archive_at"], "2026-10-11T01:00:00+08:00")
        stored = self.service.inbox.pending(DAY)
        self.assertEqual(list(stored), ["B"])
        self.assertEqual(stored["B"].submitted_at.tzinfo is not None, True)
        self.assertTrue(self.service.inbox.path("骨干甲").exists())
        self.assertEqual(self.bitable.created, [])

    def test_empty_content_is_rejected_without_storing(self):
        self.assertEqual(self.service.submit(WorkBuddySubmitRequest(
            name="骨干甲", progress="   "))["code"], EMPTY_CONTENT)
        self.assertEqual(self.service.inbox.pending(DAY), {})

    def test_submit_outside_window_is_rejected_without_storing(self):
        self.freeze(23, 55)
        result = self.service.submit(WorkBuddySubmitRequest(
            name="骨干甲", progress="半夜交"))
        self.assertEqual(result["code"], CLOSED)
        self.assertEqual(result["next_open_at"], "2026-10-11T12:00:00+08:00")
        self.assertEqual(self.service.inbox.pending(DAY), {})

    def test_person_id_must_match_the_name(self):
        self.assertEqual(self.service.submit(WorkBuddySubmitRequest(
            person_id="S001", name="骨干甲", progress="今天"))["code"],
            PERSON_ID_UNKNOWN)
        self.assertEqual(self.service.inbox.pending(DAY), {})

    # ----------------------------------------------------------- 写回

    def test_resubmit_overwrites_and_flush_writes_only_the_last(self):
        self.service.submit(WorkBuddySubmitRequest(name="基层乙", progress="第一版"))
        self.service.submit(WorkBuddySubmitRequest(name="基层乙", progress="第二版"))
        self.service.submit(WorkBuddySubmitRequest(name="基层乙", progress="第三版"))
        # 一人一天一个文件,后交的覆盖先交的
        self.assertEqual(list(self.service.inbox.pending(DAY)), ["S001"])
        stored = self.service.inbox.pending(DAY)["S001"]
        self.assertEqual(stored.progress, "第三版")

        written, issues = self.service.flush(DAY)
        self.assertEqual((written, issues), (1, {}))
        self.assertEqual(len(self.bitable.created), 1)
        self.assertEqual(self.bitable.created[0]["工作进展："], "第三版")
        self.assertIn("工作进展:第三版", self.bitable.created[0]["完整日志"])
        # 提交时间写的是学生提交那一刻,不是写回时刻
        self.assertEqual(self.bitable.created[0]["提交时间"],
                         int(stored.submitted_at.timestamp() * 1000))
        # 基层学生没有 open_id,就不写提交人字段,也不写空栏位
        self.assertNotIn("提交人", self.bitable.created[0])
        self.assertNotIn("工作困难：", self.bitable.created[0])
        # 写回成功后暂存被清掉
        self.assertEqual(self.service.inbox.pending(DAY), {})

        self.assertEqual(self.service.flush(DAY), (0, {}))
        self.assertEqual(len(self.bitable.created), 1)

    def test_flush_adopts_an_existing_row_when_the_key_was_lost(self):
        self.service.submit(WorkBuddySubmitRequest(name="骨干甲", progress="已建行"))
        submission = self.service.inbox.pending(DAY)["B"]
        stamp = int(submission.submitted_at.timestamp() * 1000)
        self.bitable.rows["logs"] = [{"record_id": "existing", "fields": {
            "姓名：": "骨干甲", "提交时间": stamp}}]

        written, issues = self.service.flush(DAY)
        self.assertEqual((written, issues), (0, {}))
        self.assertEqual(self.bitable.created, [])
        self.assertEqual(
            self.workflow_store.get_external_record(DAY, "workbuddy:log:B")[
                "record_id"], "existing")
        # 认领成功也算处理完,暂存清掉
        self.assertEqual(self.service.inbox.pending(DAY), {})

    def test_flush_records_an_issue_when_the_person_disappeared(self):
        self.service.submit(WorkBuddySubmitRequest(name="基层乙", progress="今天"))
        self.organization.persons = [person for person in self.organization.persons
                                     if person.person_id != "S001"]
        written, issues = self.service.flush(DAY)
        self.assertEqual(written, 0)
        self.assertIn("workbuddy:S001:person-missing", issues)
        # 写不回去的暂存要留着,等人补回组织表
        self.assertEqual(list(self.service.inbox.pending(DAY)), ["S001"])

    def test_flush_writes_the_submitter_field_for_people_with_open_id(self):
        self.service.submit(WorkBuddySubmitRequest(name="骨干甲", progress="有 open_id"))
        self.service.flush(DAY)
        self.assertEqual(self.bitable.created[0]["提交人"], [{"id": "ou_b"}])


class Workflow1FlushOrderTests(unittest.TestCase):
    """写回失败必须让当日 FAILED,且绝不带着缺失的日志建快照。"""

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = FileStateStore(
            self.temp.name, workflow_type="workflow1",
            snapshot_model=DailySnapshot)
        self.read_logs_calls = []

        class FailingSubmitService:
            def flush(self, target_date):
                raise RuntimeError("飞书不可用")

        self.workflow = self._build(FailingSubmitService())

    def _build(self, workbuddy):
        from workflow1.workflow import Workflow1

        def get_logs_by_date(target_date, organization=None):
            self.read_logs_calls.append(target_date)
            return [], {}

        return Workflow1(
            store=self.store,
            organization_cache=SimpleNamespace(get=lambda: SimpleNamespace(
                anomalies=[], persons=[], departments=[])),
            log_repository=SimpleNamespace(
                get_logs_by_date=get_logs_by_date,
                backfill_full_logs=lambda logs: 0),
            ai_evaluations=SimpleNamespace(),
            human_evaluations=SimpleNamespace(),
            prompt_service=SimpleNamespace(),
            messages=SimpleNamespace(),
            documents=SimpleNamespace(),
            reports=SimpleNamespace(),
            workbuddy=workbuddy,
        )

    def test_write_back_failure_fails_the_day_before_any_snapshot(self):
        run = self.workflow.start(DAY)
        self.assertEqual(run.status, "FAILED")
        self.assertEqual(self.read_logs_calls, [])
        self.assertIsNone(self.store.load_snapshot(DAY))


class WorkBuddyRouteTests(unittest.TestCase):
    def test_routes_are_registered_with_their_own_token(self):
        settings = AppSettings(
            feishu=FeishuSettings("app", "secret", "base"),
            deepseek=DeepSeekSettings("key"),
            workbuddy_token="workbuddy-token")
        runtime = Runtime(settings=settings, infrastructure=Mock(),
                          organization_cache=Mock(),
                          workflows={"workflow1_daily": WorkflowBinding(
                              name="workflow1_daily", workflow=Mock(), store=Mock(),
                              auto_advance_at="", confirmation_webhook_token="token",
                              record_handlers={})},
                          workbuddy=Mock())
        paths = create_app(settings, runtime).openapi()["paths"]
        self.assertIn("/workbuddy/v1/work-logs", paths)
        self.assertIn("/workbuddy/v1/work-logs/identity", paths)


class WorkBuddyAuthTests(unittest.TestCase):
    """独立 token 不只是最小权限:它让「入口不许碰 /admin」变成技术强制。"""

    def setUp(self):
        from fastapi.testclient import TestClient

        self.settings = AppSettings(
            feishu=FeishuSettings("app", "secret", "base"),
            deepseek=DeepSeekSettings("key"),
            admin_token="admin-token", workbuddy_token="workbuddy-token")
        self.service = Mock()
        self.service.resolve.return_value = {"code": OK, "person_id": "S001",
                                             "name": "基层乙"}
        runtime = Runtime(
            settings=self.settings, infrastructure=Mock(),
            organization_cache=Mock(),
            workflows={"workflow1_daily": WorkflowBinding(
                name="workflow1_daily", workflow=Mock(), store=Mock(),
                auto_advance_at="", confirmation_webhook_token="token",
                record_handlers={})},
            workbuddy=self.service)
        self.client = TestClient(create_app(self.settings, runtime))

    def test_identity_requires_the_workbuddy_token(self):
        path = "/workbuddy/v1/work-logs/identity"
        self.assertEqual(
            self.client.post(path, json={"name": "基层乙"}).status_code, 401)
        self.assertEqual(self.client.post(
            path, json={"name": "基层乙"},
            headers={"Authorization": "Bearer nope"}).status_code, 401)

        ok = self.client.post(path, json={"name": "基层乙"},
                              headers={"Authorization": "Bearer workbuddy-token"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["person_id"], "S001")

    def test_workbuddy_token_cannot_reach_admin_endpoints(self):
        response = self.client.get(
            "/admin/organization/persons",
            headers={"Authorization": "Bearer workbuddy-token"})
        self.assertEqual(response.status_code, 401)

    def test_submit_endpoint_returns_the_business_code_as_200(self):
        self.service.submit.return_value = {"code": EMPTY_CONTENT,
                                            "person_id": "S001"}
        response = self.client.post(
            "/workbuddy/v1/work-logs", json={"name": "基层乙", "progress": " "},
            headers={"Authorization": "Bearer workbuddy-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], EMPTY_CONTENT)


if __name__ == "__main__":
    unittest.main()

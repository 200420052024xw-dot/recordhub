"""WorkBuddy 查询接口：认人、权限、区间、评价、阶段工作、分页与独立 token。"""

from __future__ import annotations

import sys
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
from service.workbuddy_query.models import (  # noqa: E402
    CONTENT_NOT_SUPPORTED,
    DUPLICATE_NAME,
    NO_PERSON,
    OK,
    OUT_OF_SCOPE,
    QUERY_FAILED,
    RANGE_INVALID,
    RANGE_TOO_LARGE,
    ROLE_NOT_ALLOWED,
    TARGET_NOT_FOUND,
    QueryRequest,
    RangeSpec,
    Target,
)
from service.workbuddy_query.ranges import clamp_view_window, resolve_window  # noqa: E402
from service.workbuddy_query.service import WorkBuddyQueryService  # noqa: E402
from workflow1.models import (  # noqa: E402
    DailySnapshot,
    LogEvaluationState,
    UnitStatus,
    WorkBuddySubmission,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
TODAY = date(2026, 10, 14)
YESTERDAY = date(2026, 10, 13)
FIXED_NOW = datetime(2026, 10, 14, 10, 0, tzinfo=SHANGHAI)


def make_organization() -> Organization:
    persons = [
        Person(person_id="L1", name="负责人甲", role="团队负责人"),
        Person(person_id="M1", name="部长甲", role="部长", department_id="D1", leader_id="L1"),
        Person(person_id="M2", name="部长乙", role="部长", department_id="D2", leader_id="L1"),
        Person(person_id="B1", name="骨干甲", role="骨干学生", department_id="D1", leader_id="M1"),
        Person(person_id="B2", name="骨干乙", role="骨干学生", department_id="D2", leader_id="M2"),
        Person(person_id="S1", name="基层甲", role="基层学生", department_id="D1", leader_id="B1"),
        Person(person_id="S2", name="基层乙", role="基层学生", department_id="D1", leader_id="B1"),
        Person(person_id="S3", name="基层丙", role="基层学生", department_id="D2", leader_id="B2"),
    ]
    departments = [
        Department(department_id="D1", name="研发部", minister_id="M1"),
        Department(department_id="D2", name="测试部", minister_id="M2"),
    ]
    return Organization(persons=persons, departments=departments, team_leader_id="L1")


def make_log(log_id: str, person_id: str, *, progress: str = "进展") -> WorkLog:
    return WorkLog(log_id=log_id, person_id=person_id, submitted_at=FIXED_NOW,
                   progress=progress, difficulties="困难", reflection="反思", other="其他")


def make_snapshot(day: date, organization: Organization, logs,
                  evaluations=None) -> DailySnapshot:
    return DailySnapshot(
        workflow_run_id=f"run:{day.isoformat()}", target_date=day,
        organization=organization, logs=list(logs), submission_status={},
        log_evaluations=evaluations or {}, created_at=FIXED_NOW, updated_at=FIXED_NOW)


def confirmed(log_id: str, person_id: str, positive: str = "肯定", improvement: str = "改进",
              source: str = "HUMAN") -> LogEvaluationState:
    return LogEvaluationState(log_id=log_id, person_id=person_id, evaluator_id="B1",
                              status=UnitStatus.CONFIRMED, positive_final=positive,
                              improvement_final=improvement, source=source)


class FakeOrganizationCache:
    def __init__(self, organization): self.organization = organization
    def get(self): return self.organization


class FakeLogRepository:
    def __init__(self, logs_by_day=None): self.logs_by_day = logs_by_day or {}
    def logs_in_window(self, start, end, organization=None):
        result, day = {}, start
        while day <= end:
            result[day] = (list(self.logs_by_day.get(day, [])), {})
            day += timedelta(days=1)
        return result


class FakeInbox:
    def __init__(self, pending=None): self._pending = pending or {}
    def pending(self, target_date): return dict(self._pending)


class FakeSnapshotStore:
    def __init__(self, snapshots=None): self.snapshots = snapshots or {}
    def load_check_snapshot(self, day): return self.snapshots.get(day)


class FakeRebuilder:
    def __init__(self, snapshots=None): self.snapshots = snapshots or {}
    def rebuild(self, start, end):
        return {day: value for day, value in self.snapshots.items() if start <= day <= end}


class FakeBitable:
    def __init__(self, rows=None): self.rows = rows or {}
    def list_records(self, table_id, **kwargs): return list(self.rows.get(table_id, []))


def build_service(*, organization=None, logs_by_day=None, snapshots=None, pending=None,
                  tables=None, bitable=None, page_size=50) -> WorkBuddyQueryService:
    organization = organization or make_organization()
    return WorkBuddyQueryService(
        organization_cache=FakeOrganizationCache(organization),
        log_repository=FakeLogRepository(logs_by_day),
        bitable=bitable or FakeBitable(),
        tables=tables or TableConfig(tables={}),
        inbox=FakeInbox(pending),
        snapshot_store=FakeSnapshotStore(snapshots),
        rebuilder=FakeRebuilder(snapshots),
        cursor_secret="secret",
        clock=lambda: FIXED_NOW,
        page_size=page_size,
    )


def query(service: WorkBuddyQueryService, **kwargs) -> dict:
    return service.query(QueryRequest(**kwargs))


class RangeResolverTests(unittest.TestCase):
    def test_shortcuts(self) -> None:
        cases = {
            "today": (TODAY, TODAY),
            "yesterday": (YESTERDAY, YESTERDAY),
            "day_before_yesterday": (date(2026, 10, 12), date(2026, 10, 12)),
            "this_week": (date(2026, 10, 12), TODAY),
            "last_week": (date(2026, 10, 5), date(2026, 10, 11)),
            "last_7_days": (date(2026, 10, 8), TODAY),
            "last_30_days": (date(2026, 9, 15), TODAY),
            "this_month": (date(2026, 10, 1), TODAY),
            "last_month": (date(2026, 9, 1), date(2026, 9, 30)),
        }
        for shortcut, expected in cases.items():
            with self.subTest(shortcut=shortcut):
                self.assertEqual(
                    resolve_window(RangeSpec(shortcut=shortcut), today=TODAY), expected)

    def test_explicit_range(self) -> None:
        spec = RangeSpec(**{"from": "2026-10-07", "to": "2026-10-13"})
        self.assertEqual(resolve_window(spec, today=TODAY),
                         (date(2026, 10, 7), YESTERDAY))

    def test_invalid_ranges(self) -> None:
        for spec in (None,
                     RangeSpec(shortcut="nonsense"),
                     RangeSpec(),
                     RangeSpec(**{"from": "2026-10-13", "to": "2026-10-07"}),
                     RangeSpec(shortcut="last_7_days", **{"from": "2026-10-07"})):
            with self.subTest(spec=spec):
                with self.assertRaises(Exception) as ctx:
                    resolve_window(spec, today=TODAY)
                self.assertEqual(ctx.exception.code, RANGE_INVALID)

    def test_window_cap(self) -> None:
        too_long = RangeSpec(**{"from": "2026-09-01", "to": "2026-10-13"})
        with self.assertRaises(Exception) as ctx:
            resolve_window(too_long, today=TODAY)
        self.assertEqual(ctx.exception.code, RANGE_TOO_LARGE)
        exact = RangeSpec(**{"from": "2026-09-13", "to": "2026-10-13"})
        self.assertEqual(resolve_window(exact, today=TODAY),
                         (date(2026, 9, 13), YESTERDAY))

    def test_view_clamp_excludes_today(self) -> None:
        self.assertEqual(clamp_view_window(date(2026, 10, 7), TODAY, today=TODAY),
                         (date(2026, 10, 7), YESTERDAY))
        with self.assertRaises(Exception) as ctx:
            clamp_view_window(TODAY, TODAY, today=TODAY)
        self.assertEqual(ctx.exception.code, RANGE_INVALID)


class IdentityTests(unittest.TestCase):
    def test_no_person(self) -> None:
        result = build_service().identity("查无此人")
        self.assertEqual(result["code"], NO_PERSON)

    def test_duplicate_name(self) -> None:
        organization = make_organization()
        organization.persons.append(
            Person(person_id="S9", name="基层甲", role="基层学生", department_id="D2"))
        result = build_service(organization=organization).identity("基层甲")
        self.assertEqual(result["code"], DUPLICATE_NAME)

    def test_ok_reports_scope(self) -> None:
        service = build_service()
        expected = {"基层甲": ("基层学生", "研发部", "self"),
                    "骨干甲": ("骨干学生", "研发部", "group"),
                    "部长甲": ("部长", "研发部", "department"),
                    "负责人甲": ("团队负责人", "", "all")}
        for name, (role, department, scope) in expected.items():
            with self.subTest(name=name):
                result = service.identity(name)
                self.assertEqual(result["code"], OK)
                self.assertEqual((result["role"], result["department_name"],
                                  result["scope"]), (role, department, scope))


class PermissionTests(unittest.TestCase):
    def _code(self, person_id: str, **target) -> str:
        result = query(build_service(), person_id=person_id, mode="view",
                       target=Target(**target), range=RangeSpec(shortcut="yesterday"))
        return result["code"]

    def test_layer_matrix(self) -> None:
        cases = [
            ("S1", {"type": "self"}, OK),
            ("S1", {"type": "person", "name": "基层乙"}, OUT_OF_SCOPE),
            ("S1", {"type": "department", "name": "研发部"}, OUT_OF_SCOPE),
            ("B1", {"type": "group", "name": "骨干甲"}, OK),
            ("B1", {"type": "person", "name": "基层甲"}, OK),
            ("B1", {"type": "person", "name": "基层丙"}, OUT_OF_SCOPE),
            ("B1", {"type": "group", "name": "骨干乙"}, OUT_OF_SCOPE),
            ("B1", {"type": "department", "name": "研发部"}, OUT_OF_SCOPE),
            ("M1", {"type": "department", "name": "研发部"}, OK),
            ("M1", {"type": "department", "name": "测试部"}, OUT_OF_SCOPE),
            ("L1", {"type": "department", "name": "测试部"}, OK),
        ]
        for person_id, target, expected in cases:
            with self.subTest(person_id=person_id, target=target):
                self.assertEqual(self._code(person_id, **target), expected)

    def test_unknown_target_is_not_found(self) -> None:
        for target in ({"type": "person", "name": "查无此人"},
                       {"type": "group", "name": "部长甲"},
                       {"type": "department", "name": "不存在部"}):
            with self.subTest(target=target):
                self.assertEqual(self._code("L1", **target), TARGET_NOT_FOUND)


class ViewTests(unittest.TestCase):
    def test_view_returns_logs_and_final_evaluation(self) -> None:
        organization = make_organization()
        log = make_log("L1", "S1")
        snapshots = {YESTERDAY: make_snapshot(
            YESTERDAY, organization, [log],
            {"L1": confirmed("L1", "S1", source="HUMAN")})}
        service = build_service(organization=organization,
                                logs_by_day={YESTERDAY: [log]}, snapshots=snapshots)
        result = query(service, person_id="B1", mode="view",
                       target=Target(type="group", name="骨干甲"),
                       range=RangeSpec(shortcut="last_7_days"))
        self.assertEqual(result["code"], OK)
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["item_id"], "L1")
        self.assertEqual(item["name"], "基层甲")
        self.assertEqual(item["evaluation"]["source"], "人工")

    def test_view_excludes_today_and_unconfirmed_evaluation(self) -> None:
        organization = make_organization()
        today_log = make_log("L9", "S1")
        pending_state = LogEvaluationState(log_id="L9", person_id="S1", evaluator_id="B1")
        snapshots = {TODAY: make_snapshot(TODAY, organization, [today_log],
                                          {"L9": pending_state})}
        service = build_service(organization=organization,
                                logs_by_day={TODAY: [today_log]}, snapshots=snapshots)
        result = query(service, person_id="B1", mode="view",
                       target=Target(type="group", name="骨干甲"),
                       range=RangeSpec(shortcut="last_7_days"))
        self.assertEqual(result["code"], OK)
        self.assertTrue(all(item["date"] != TODAY.isoformat() for item in result["items"]))

    def test_view_today_only_is_invalid(self) -> None:
        result = query(build_service(), person_id="B1", mode="view",
                       range=RangeSpec(shortcut="today"))
        self.assertEqual(result["code"], RANGE_INVALID)


class ReviewTests(unittest.TestCase):
    def _submission(self) -> WorkBuddySubmission:
        return WorkBuddySubmission(
            submission_id="SUB1", person_id="S1", name="基层甲", role="基层学生",
            department_name="研发部", submitted_at=FIXED_NOW, target_date=TODAY,
            progress="今天进展", difficulties="", reflection="", other="")

    def test_role_gate(self) -> None:
        result = query(build_service(), person_id="S1", mode="review")
        self.assertEqual(result["code"], ROLE_NOT_ALLOWED)

    def test_backbone_reviews_reports_raw_log(self) -> None:
        service = build_service(pending={"S1": self._submission()})
        result = query(service, person_id="B1", mode="review")
        self.assertEqual(result["code"], OK)
        names = [item["name"] for item in result["items"]]
        self.assertEqual(names, ["基层甲"])
        item = result["items"][0]
        self.assertEqual(item["item_id"], "SUB1")
        self.assertEqual(item["log"]["progress"], "今天进展")
        self.assertNotIn("evaluation", item)

    def test_leader_reviews_all_subordinates(self) -> None:
        result = query(build_service(), person_id="L1", mode="review")
        self.assertEqual(result["code"], OK)
        self.assertEqual(sorted(r["name"] for r in result["roster"]), ["部长乙", "部长甲"])


def stage_tables() -> TableConfig:
    return TableConfig(tables={
        "stage_report": TableDefinition(table_id="tbl_stage", fields={
            "task_id": "任务编号", "analysis_date": "日期",
            "minister_ref": "部长", "progress": "阶段工作总结"}),
        "monthly_department_report": TableDefinition(table_id="tbl_monthly", fields={
            "task_id": "任务编号", "analysis_date": "日期", "minister_ref": "部长",
            "personnel_s05": "名单", "ideas_s06": "idea", "meeting_s07": "例会"}),
        "team_analysis": TableDefinition(table_id="tbl_team", fields={
            "task_id": "任务编号", "analysis_date": "日期",
            "technology_s08": "公共技术", "training_s09": "培训"}),
    })


def stage_bitable() -> FakeBitable:
    when = "2026-10-13T00:00:00+08:00"
    return FakeBitable({
        "tbl_stage": [
            {"record_id": "sr1", "fields": {"任务编号": "run:D1", "日期": when,
                                            "部长": "M1", "阶段工作总结": "总结一"}},
            {"record_id": "sr2", "fields": {"任务编号": "run:D2", "日期": when,
                                            "部长": "M2", "阶段工作总结": "总结二"}},
            {"record_id": "sr3", "fields": {"任务编号": "run:TEAM", "日期": when,
                                            "部长": "L1", "阶段工作总结": "团队总结"}},
        ],
        "tbl_team": [
            {"record_id": "tm1", "fields": {"任务编号": "run:TEAM", "日期": when,
                                            "公共技术": {"text": "技术", "link": "https://x"},
                                            "培训": "培训建议"}},
        ],
    })


class StageTests(unittest.TestCase):
    def _run(self, person_id: str, **target) -> dict:
        service = build_service(tables=stage_tables(), bitable=stage_bitable())
        return query(service, person_id=person_id, mode="view", content="stage",
                     target=Target(**target) if target else None,
                     range=RangeSpec(shortcut="last_7_days"))

    def test_role_gate(self) -> None:
        for person_id in ("S1", "B1"):
            with self.subTest(person_id=person_id):
                self.assertEqual(self._run(person_id)["code"], CONTENT_NOT_SUPPORTED)

    def test_minister_sees_only_own_department(self) -> None:
        result = self._run("M1", type="department", name="研发部")
        self.assertEqual(result["code"], OK)
        self.assertEqual([item["name"] for item in result["items"]], ["研发部"])

    def test_leader_sees_all_and_team(self) -> None:
        result = self._run("L1")
        self.assertEqual(result["code"], OK)
        names = {item["name"] for item in result["items"]}
        self.assertIn("团队", names)
        self.assertIn("研发部", names)
        self.assertIn("测试部", names)
        sections = [section for item in result["items"] if item["name"] == "团队"
                    for section in item["sections"]]
        self.assertIn({"label": "公共技术", "text": "技术", "url": "https://x"}, sections)

    def test_leader_narrows_to_requested_department(self) -> None:
        # 负责人指定部门时，只返回该部门，不再捎带其它部门和团队。
        result = self._run("L1", type="department", name="研发部")
        self.assertEqual(result["code"], OK)
        self.assertEqual([item["name"] for item in result["items"]], ["研发部"])

    def test_absent_stage_tables_do_not_raise(self) -> None:
        service = build_service(tables=TableConfig(tables={}), bitable=FakeBitable())
        result = query(service, person_id="L1", mode="view", content="stage",
                       range=RangeSpec(shortcut="last_7_days"))
        self.assertEqual(result["code"], OK)
        self.assertEqual(result["items"], [])


class QueryFailureTests(unittest.TestCase):
    def test_data_source_failure_becomes_query_failed(self) -> None:
        class Boom:
            def logs_in_window(self, *args, **kwargs):
                raise RuntimeError("数据源炸了")

        service = WorkBuddyQueryService(
            organization_cache=FakeOrganizationCache(make_organization()),
            log_repository=Boom(), bitable=FakeBitable(),
            tables=TableConfig(tables={}), inbox=FakeInbox(),
            snapshot_store=FakeSnapshotStore(), rebuilder=FakeRebuilder(),
            cursor_secret="secret", clock=lambda: FIXED_NOW)
        with self.assertLogs("service.workbuddy_query.service", level="ERROR"):
            result = service.query(QueryRequest(
                person_id="B1", mode="view", target=Target(type="group", name="骨干甲"),
                range=RangeSpec(shortcut="last_7_days")))
        self.assertEqual(result["code"], QUERY_FAILED)


class CursorTests(unittest.TestCase):
    def _two_logs(self):
        organization = make_organization()
        logs = [make_log("L1", "S1"), make_log("L2", "S2")]
        return build_service(organization=organization,
                             logs_by_day={YESTERDAY: logs}, page_size=1)

    def test_pagination_round_trip(self) -> None:
        service = self._two_logs()
        first = query(service, person_id="B1", mode="view",
                      target=Target(type="group", name="骨干甲"),
                      range=RangeSpec(shortcut="last_7_days"))
        self.assertEqual(len(first["items"]), 1)
        self.assertIsNotNone(first["next_cursor"])
        second = query(service, person_id="B1", mode="view",
                       target=Target(type="group", name="骨干甲"),
                       range=RangeSpec(shortcut="last_7_days"),
                       cursor=first["next_cursor"])
        self.assertEqual(len(second["items"]), 1)
        self.assertIsNone(second["next_cursor"])

    def test_tampered_cursor_fails_softly(self) -> None:
        result = query(self._two_logs(), person_id="B1", mode="view",
                       target=Target(type="group", name="骨干甲"),
                       range=RangeSpec(shortcut="last_7_days"), cursor="garbage.sig")
        self.assertEqual(result["code"], QUERY_FAILED)


class TokenSeparationTests(unittest.TestCase):
    def _settings(self, query_token: str = "query-token") -> AppSettings:
        return AppSettings(
            feishu=FeishuSettings("app", "secret", "base"),
            deepseek=DeepSeekSettings("key"),
            admin_token="admin-token", workbuddy_token="workbuddy-token",
            query_token=query_token)

    def _client(self, settings: AppSettings):
        from fastapi.testclient import TestClient
        binding = WorkflowBinding(name="workflow1_daily", workflow=Mock(), store=Mock(),
                                  auto_advance_at="", confirmation_webhook_token="token",
                                  record_handlers={})
        service = Mock()
        service.query.return_value = {"code": OK, "items": []}
        service.identity.return_value = {"code": OK, "person_id": "S1"}
        runtime = Runtime(settings=settings, infrastructure=Mock(),
                          organization_cache=Mock(),
                          workflows={binding.name: binding}, query=service)
        return TestClient(create_app(settings, runtime))

    def test_query_endpoints_require_the_query_token(self) -> None:
        client = self._client(self._settings())
        body = {"person_id": "B1", "mode": "view"}
        for headers in ({}, {"Authorization": "Bearer admin-token"},
                        {"Authorization": "Bearer workbuddy-token"}):
            with self.subTest(headers=headers):
                self.assertEqual(
                    client.post("/workbuddy/v1/queries", json=body, headers=headers)
                    .status_code, 401)
        ok = client.post("/workbuddy/v1/queries", json=body,
                         headers={"Authorization": "Bearer query-token"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["code"], OK)

    def test_query_token_cannot_reach_admin(self) -> None:
        client = self._client(self._settings())
        response = client.get("/admin/organization/persons",
                              headers={"Authorization": "Bearer query-token"})
        self.assertEqual(response.status_code, 401)

    def test_missing_query_token_configures_503(self) -> None:
        client = self._client(self._settings(query_token=""))
        response = client.post("/workbuddy/v1/queries", json={"person_id": "B1", "mode": "view"},
                               headers={"Authorization": "Bearer anything"})
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()

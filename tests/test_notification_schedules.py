"""Notification gates for the configured daily and periodic cycles."""

import sys
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config import load_schedule_config
from data.store import utc_now
from workflow1.workflow import Workflow1
from workflow2.models import CycleRun, DepartmentResult
from workflow2.workflow import Workflow2


def _cycle_run(kind: str, day: date, **overrides) -> CycleRun:
    values = dict(run_id=str(uuid5(NAMESPACE_URL, f"recordhub-workflow2:{kind}:{day}")),
                  kind=kind, scheduled_date=day, start_date=day - timedelta(days=2),
                  end_date=day - timedelta(days=1),
                  created_at=datetime.now(timezone.utc),
                  updated_at=datetime.now(timezone.utc))
    values.update(overrides)
    return CycleRun(**values)


class NotificationScheduleTests(unittest.TestCase):
    def test_review_forms_are_sent_to_the_matching_roles(self):
        schedules = load_schedule_config("config/schedules.toml")
        options = schedules.workflows["workflow1_daily"].options
        self.assertEqual(
            schedules.workflows["daily_log_reminder"].options["form_url"],
            "https://jwxnd3ayslt.feishu.cn/share/base/form/shrcnomQ0yUYQGvAODCOC3bZ5sf",
        )
        people = {
            "M": SimpleNamespace(name="部长甲", role="部长", open_id="ou_m"),
            "B": SimpleNamespace(name="骨干乙", role="骨干学生", open_id="ou_b"),
            "C": SimpleNamespace(name="骨干丙", role="骨干学生", open_id="ou_c"),
            "S": SimpleNamespace(name="基层丁", role="基层学生", open_id=""),
        }
        snapshot = SimpleNamespace(
            organization=SimpleNamespace(person_map=lambda: people),
            evaluators={
                "M": SimpleNamespace(log_ids=["L1"]),
                "B": SimpleNamespace(log_ids=["L2"]),
            },
            log_evaluations={
                "L1": SimpleNamespace(person_id="C"),
                "L2": SimpleNamespace(person_id="S"),
            },
        )
        workflow = Workflow1.__new__(Workflow1)
        workflow.review_form_urls = {
            "部长": options["minister_review_form_url"],
            "骨干学生": options["backbone_review_form_url"],
        }
        workflow.store = SimpleNamespace(load_snapshot=lambda target: snapshot,
                                         update_snapshot=lambda target, mutate: mutate(snapshot))
        workflow._notify_once = Mock()
        workflow._record_issue = Mock()

        workflow._notify_evaluators(date(2026, 10, 5))

        self.assertEqual(workflow._notify_once.call_count, 2)
        minister, backbone = workflow._notify_once.call_args_list
        self.assertEqual(minister.args[2], "ou_m")
        self.assertIn(options["minister_review_form_url"], minister.args[3])
        self.assertNotIn(options["backbone_review_form_url"], minister.args[3])
        self.assertEqual(backbone.args[2], "ou_b")
        self.assertIn(options["backbone_review_form_url"], backbone.args[3])
        self.assertNotIn(options["minister_review_form_url"], backbone.args[3])

    def test_daily_teacher_notification_waits_until_eight(self):
        workflow = Workflow1.__new__(Workflow1)
        workflow._lock = threading.RLock()
        workflow.notify_at = "08:00"
        workflow.auto_advance_at = "19:00"
        workflow.store = SimpleNamespace(
            load_snapshot=lambda target: SimpleNamespace(evaluations_published=True))
        workflow._notify_evaluators = Mock()

        class At(datetime):
            current = datetime(2026, 10, 6, 7, 59, tzinfo=timezone.utc)

            @classmethod
            def now(cls, tz=None):
                return cls.current.astimezone(tz)

        target = date(2026, 10, 5)
        with patch("workflow1.workflow.datetime", At):
            At.current = datetime(2026, 10, 5, 23, 59, tzinfo=timezone.utc)
            workflow.notify_evaluators_if_due(target)
            workflow._notify_evaluators.assert_not_called()
            At.current = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
            workflow.notify_evaluators_if_due(target)
            workflow._notify_evaluators.assert_called_once_with(target)
            At.current = datetime(2026, 10, 6, 11, 0, tzinfo=timezone.utc)
            workflow.notify_evaluators_if_due(target)
            workflow._notify_evaluators.assert_called_once()

    def test_department_reports_wait_until_evaluation_day_at_ten_pm(self):
        workflow = Workflow1.__new__(Workflow1)
        workflow.report_notify_at = "22:00"
        workflow.store = SimpleNamespace(load_snapshot=Mock())
        workflow._notify_documents = Mock()
        snapshot = SimpleNamespace(cloud_objects={})
        objects = {"ready": object()}

        class At(datetime):
            current = datetime(2026, 10, 6, 13, 59, tzinfo=timezone.utc)

            @classmethod
            def now(cls, tz=None):
                return cls.current.astimezone(tz)

        target = date(2026, 10, 5)
        with patch("workflow1.workflow.datetime", At):
            workflow.notify_documents_if_due(target, snapshot=snapshot, objects=objects)
            workflow._notify_documents.assert_not_called()
            At.current = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)
            workflow.notify_documents_if_due(target, snapshot=snapshot, objects=objects)
            workflow._notify_documents.assert_called_once_with(target, snapshot, objects)

    def test_monthly_and_weekly_notifications_wait_for_their_times(self):
        schedules = load_schedule_config("config/schedules.toml")
        day = date(2026, 10, 5)
        runs = {}
        for kind in ("monthly", "weekly"):
            run_id = str(uuid5(NAMESPACE_URL, f"recordhub-workflow2:{kind}:{day}"))
            run = CycleRun(run_id=run_id, kind=kind, scheduled_date=day,
                start_date=date(2026, 9, 1), end_date=date(2026, 10, 4),
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc))
            if kind == "monthly":
                run.departments["D"] = DepartmentResult(
                    department_id="D", minister_id="M", analysis_published=True,
                    draft_text={code: "draft" for code in ("S05", "S06", "S07")})
            else:
                run.status = "COMPLETED"
                run.document_urls = {"S08": "https://example.com/8",
                                     "S09": "https://example.com/9"}
            runs[kind] = run
        workflow = Workflow2.__new__(Workflow2)
        workflow.lock = threading.RLock()
        workflow.schedules = schedules
        workflow.store = SimpleNamespace(
            load=lambda run_id: next((run for run in runs.values()
                if run.run_id == run_id), None), save=lambda run: None)
        workflow.organization = SimpleNamespace(get=lambda: SimpleNamespace(
            person_map=lambda: {"M": SimpleNamespace(open_id="ou_m"),
                                "T": SimpleNamespace(open_id="ou_t")},
            team_leader_id="T"))
        workflow.messages = SimpleNamespace(send_text=Mock(
            return_value={"message_id": "msg"}))

        with patch("workflow2.workflow.utc_now") as now:
            now.return_value = datetime(2026, 10, 5, 3, 59, tzinfo=timezone.utc)
            workflow.notify_due("monthly", day)
            self.assertEqual(workflow.messages.send_text.call_count, 0)
            now.return_value = datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)
            workflow.notify_due("monthly", day)
            workflow.notify_due("monthly", day)
            self.assertEqual(workflow.messages.send_text.call_count, 1)
            runs["monthly"].status = "COMPLETED"
            runs["monthly"].document_urls = {
                code: f"https://example.com/{code}" for code in ("S05", "S06", "S07")}
            workflow.notify_due("monthly", day)
            workflow.notify_due("monthly", day)
            self.assertEqual(workflow.messages.send_text.call_count, 2)
            now.return_value = datetime(2026, 10, 5, 0, 59, tzinfo=timezone.utc)
            workflow.notify_due("weekly", day)
            self.assertEqual(workflow.messages.send_text.call_count, 2)
            now.return_value = datetime(2026, 10, 5, 1, 0, tzinfo=timezone.utc)
            workflow.notify_due("weekly", day)
            workflow.notify_due("weekly", day)
            self.assertEqual(workflow.messages.send_text.call_count, 3)

    def test_stage_notifies_the_minister_at_eight_even_though_it_ran_at_four(self):
        """阶段分析凌晨跑完，但确认通知要等 08:00，不能半夜吵部长。"""
        schedules = load_schedule_config("config/schedules.toml")
        day = date(2026, 10, 5)
        run = _cycle_run("stage", day, status="WAITING_CONFIRMATION")
        run.departments["D"] = DepartmentResult(
            department_id="D", minister_id="M", analysis_published=True,
            draft_text={"S04": "draft"})
        runs = {run.run_id: run}
        workflow = Workflow2.__new__(Workflow2)
        workflow.lock = threading.RLock()
        workflow.schedules = schedules
        workflow.store = SimpleNamespace(
            load=lambda run_id: runs.get(run_id), save=lambda state: None)
        workflow.organization = SimpleNamespace(get=lambda: SimpleNamespace(
            person_map=lambda: {"M": SimpleNamespace(open_id="ou_m"),
                                "T": SimpleNamespace(open_id="ou_t")},
            team_leader_id="T"))
        workflow.messages = SimpleNamespace(send_text=Mock(
            return_value={"message_id": "msg"}))

        with patch("workflow2.workflow.utc_now") as now:
            now.return_value = datetime(2026, 10, 4, 23, 30, tzinfo=timezone.utc)  # 07:30 北京
            workflow.notify_due("stage", day)
            self.assertEqual(workflow.messages.send_text.call_count, 0)
            now.return_value = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)    # 08:00 北京
            workflow.notify_due("stage", day)
            workflow.notify_due("stage", day)   # 幂等：只发一次
            self.assertEqual(workflow.messages.send_text.call_count, 1)
            self.assertEqual(workflow.messages.send_text.call_args.args[0], "ou_m")

    def test_stage_report_reaches_the_leader_at_ten_pm_only(self):
        """负责人固定 22:00 收报告：部长早确认了也等到 22:00，凌晨/白天都不推。"""
        schedules = load_schedule_config("config/schedules.toml")
        day = date(2026, 10, 5)
        run = _cycle_run("stage", day, status="COMPLETED")
        run.document_urls = {"S04": "https://example.com/s04"}
        runs = {run.run_id: run}
        workflow = Workflow2.__new__(Workflow2)
        workflow.lock = threading.RLock()
        workflow.schedules = schedules
        workflow.store = SimpleNamespace(load=lambda run_id: runs.get(run_id),
                                         save=lambda state: None)
        workflow.organization = SimpleNamespace(get=lambda: SimpleNamespace(
            person_map=lambda: {"M": SimpleNamespace(open_id="ou_m"),
                                "T": SimpleNamespace(open_id="ou_t")},
            team_leader_id="T"))
        workflow.messages = SimpleNamespace(send_text=Mock(
            return_value={"message_id": "msg"}))

        with patch("workflow2.workflow.utc_now") as now:
            now.return_value = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)   # 北京 14:00
            workflow.notify_due("stage", day)
            self.assertEqual(workflow.messages.send_text.call_count, 0)
            now.return_value = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)  # 北京 22:00
            workflow.notify_due("stage", day)
            workflow.notify_due("stage", day)     # 幂等：只发一次
            self.assertEqual(workflow.messages.send_text.call_count, 1)
            self.assertEqual(workflow.messages.send_text.call_args.args[0], "ou_t")

    def test_blocked_runs_retry_hourly_with_a_bounded_budget(self):
        """被阻塞的任务整点重试，初次之外最多 retry_limit 次，之后不再自动重试。"""
        workflow = Workflow2.__new__(Workflow2)
        workflow.retry_interval_minutes = 60
        workflow.retry_limit = 3
        run = _cycle_run("stage", date(2026, 10, 5), status="BLOCKED",
                         last_error="ValueError: 材料不完整")

        workflow._schedule_retry(run)
        self.assertEqual(run.retry_count, 1)
        self.assertGreaterEqual(run.retry_after - utc_now(), timedelta(minutes=59))
        self.assertFalse(workflow._retry_due(run, utc_now()))
        self.assertTrue(workflow._retry_due(run, utc_now() + timedelta(minutes=61)))

        workflow._schedule_retry(run)
        workflow._schedule_retry(run)
        self.assertEqual(run.retry_count, 3)
        self.assertTrue(workflow._retry_due(run, utc_now() + timedelta(hours=2)))

        workflow._schedule_retry(run)          # 第 4 次：超出上限，放弃
        self.assertEqual(run.retry_count, 4)
        self.assertIsNone(run.retry_after)
        self.assertFalse(workflow._retry_due(run, utc_now() + timedelta(days=1)))

    def test_manual_retry_resets_the_budget_and_only_touches_blocked_runs(self):
        """管理员手动重试：无视重试预算、只挑卡住的、顺带把预算清零。"""
        blocked = _cycle_run("stage", date(2026, 10, 5), status="BLOCKED",
                             retry_count=4, last_error="ValueError: 材料不完整")
        waiting = _cycle_run("stage", date(2026, 10, 8),
                             status="WAITING_CONFIRMATION")
        saved = []
        workflow = Workflow2.__new__(Workflow2)
        workflow.lock = threading.RLock()
        workflow.store = SimpleNamespace(pending=lambda: [blocked, waiting],
                                         save=lambda run: saved.append(run))
        workflow.resume = Mock(side_effect=lambda run_id: run_id)

        self.assertEqual(workflow.retry_blocked("stage"), [blocked.run_id])
        workflow.resume.assert_called_once_with(blocked.run_id)
        self.assertEqual(saved, [blocked])
        self.assertEqual(blocked.retry_count, 0)
        self.assertIsNone(blocked.retry_after)
        with self.assertRaises(ValueError):
            workflow.retry_blocked("quarterly")

    def test_recover_skips_blocked_runs_that_are_waiting_for_their_slot(self):
        waiting = _cycle_run("monthly", date(2026, 10, 5), status="BLOCKED",
                             retry_count=1,
                             retry_after=utc_now() + timedelta(minutes=30))
        due = _cycle_run("monthly", date(2026, 10, 5), status="BLOCKED",
                         retry_count=1, retry_after=utc_now() - timedelta(minutes=1))
        workflow = Workflow2.__new__(Workflow2)
        workflow.retry_interval_minutes = 60
        workflow.retry_limit = 3
        workflow.lock = threading.RLock()
        workflow.store = SimpleNamespace(activate=lambda today: date(2026, 1, 1),
                                         pending=lambda: [waiting, due],
                                         load=lambda run_id: None)
        workflow.resume = Mock(side_effect=lambda run_id: run_id)
        workflow.scheduled = Mock(return_value=None)

        self.assertEqual(workflow.recover(), [due.run_id])

    def test_recover_starts_the_cycle_that_never_started(self):
        """错过计划时刻的那一轮要补建；今天已经有记录的周期不重复。"""
        today = utc_now().astimezone(ZoneInfo("Asia/Shanghai")).date()
        existing = _cycle_run("monthly", today)
        workflow = Workflow2.__new__(Workflow2)
        workflow.retry_interval_minutes = 60
        workflow.retry_limit = 3
        workflow.lock = threading.RLock()
        workflow.store = SimpleNamespace(
            activate=lambda day: date(2026, 1, 1),
            pending=lambda: [existing],
            load=lambda run_id: existing if run_id == existing.run_id else None,
            save=lambda run: None)
        workflow.resume = Mock(return_value=existing)
        workflow.scheduled = Mock(return_value=None)

        self.assertEqual(workflow.recover(), [existing])
        # 月报今天已经跑过，不该被重复补建；stage / weekly 今天没记录，补上。
        self.assertEqual([call.args[0] for call in workflow.scheduled.call_args_list],
                         ["stage", "weekly"])


if __name__ == "__main__":
    unittest.main()

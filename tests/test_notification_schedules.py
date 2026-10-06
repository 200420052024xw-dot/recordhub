"""Notification gates for the configured daily and periodic cycles."""

import sys
import threading
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import NAMESPACE_URL, uuid5

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config import load_schedule_config
from workflow1.workflow import Workflow1
from workflow2.models import CycleRun, DepartmentResult
from workflow2.workflow import Workflow2


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


if __name__ == "__main__":
    unittest.main()

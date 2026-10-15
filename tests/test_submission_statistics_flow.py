from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from test_workflow1_pipeline import DAY, sample_snapshot
from data import FileStateStore
from workflow1.models import DailySnapshot, WorkflowStatus
from workflow1.models import WorkBuddySubmission
from workflow1.workbuddy_inbox import WorkBuddyInbox
from workflow1.workflow import Workflow1


class IndependentStatisticsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = FileStateStore(directory.name, snapshot_model=DailySnapshot)
        self.snapshot = sample_snapshot()
        self.organization = Mock()
        self.organization.get.return_value = self.snapshot.organization
        self.logs = Mock()
        self.logs.get_logs_by_date.return_value = (self.snapshot.logs, {})
        self.statistics = Mock()
        self.statistics.publish.return_value = {
            "target_date": str(DAY), "status": "complete", "expected": 3, "written": 3, "issues": {}}
        self.workflow = Workflow1(store=self.store, organization_cache=self.organization,
            log_repository=self.logs, ai_evaluations=Mock(), human_evaluations=Mock(),
            prompt_service=Mock(), messages=Mock(), documents=Mock(), reports=Mock(),
            statistics=self.statistics)

    def test_standalone_run_needs_no_snapshot_and_does_not_evaluate_or_report(self):
        result = self.workflow.refresh_submission_statistics(DAY)
        self.assertEqual(result["status"], "complete")
        self.statistics.publish.assert_called_once_with(DAY, self.snapshot.organization, self.snapshot.logs)
        self.assertIsNone(self.store.load_workflow(DAY))
        self.workflow.ai_evaluations.publish.assert_not_called()
        self.workflow.prompt_service.execute.assert_not_called()
        self.workflow.documents.advance.assert_not_called()
        self.workflow.reports.publish.assert_not_called()

    def test_completed_workflow_still_repairs_statistics_without_reopening(self):
        self.store.get_or_create_workflow(DAY)
        self.store.save_snapshot(self.snapshot)
        self.store.set_status(DAY, WorkflowStatus.COMPLETED)
        self.workflow.refresh_submission_statistics(DAY)
        result = self.workflow.start(DAY)
        self.statistics.publish.assert_called_once()
        self.assertEqual(result.status, WorkflowStatus.COMPLETED)
        self.workflow.ai_evaluations.publish.assert_not_called()

    def test_evaluation_failure_occurs_after_statistics_finished(self):
        self.workflow._process_logs = Mock(side_effect=RuntimeError("AI 不可用"))
        run = self.workflow.start(DAY)
        self.assertEqual(run.status, WorkflowStatus.FAILED)
        self.statistics.publish.assert_called_once()

    def test_invalid_evaluation_organization_does_not_block_statistics(self):
        self.snapshot.organization.anomalies.append("评价关系不完整")
        run = self.workflow.start(DAY)
        self.assertEqual(run.status, WorkflowStatus.FAILED)
        self.statistics.publish.assert_called_once()

    def test_outside_window_submission_is_a_notice_not_an_issue(self):
        self.logs.get_logs_by_date.return_value = (
            self.snapshot.logs,
            {"log:L9:outside-window": "日志提交时间不在开放窗口内，本条未纳入处理"})
        result = self.workflow.refresh_submission_statistics(DAY)
        # Refused by design, so it must not turn a complete write into a
        # partial one.
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["written"], 3)
        self.assertEqual(result["issues"], {})
        self.assertIn("log:L9:outside-window", result["notices"])

    def test_outside_window_is_logged_as_info_so_it_never_alerts(self):
        self.logs.get_logs_by_date.return_value = (
            self.snapshot.logs,
            {"log:L9:outside-window": "日志提交时间不在开放窗口内，本条未纳入处理"})
        with self.assertLogs("workflow1.workflow", level="INFO") as captured:
            self.workflow.refresh_submission_statistics(DAY)
        # ERROR records are what the administrator gets paged for.
        self.assertEqual(
            [record for record in captured.records if record.levelno >= logging.ERROR], [])
        self.assertTrue(any("submission_statistics_notice" in record.getMessage()
                            for record in captured.records))

    def test_other_read_issues_still_make_the_run_partial(self):
        self.logs.get_logs_by_date.return_value = (
            self.snapshot.logs, {"log:L9:unknown-submitter": "无法唯一匹配有效人员"})
        result = self.workflow.refresh_submission_statistics(DAY)
        # Guessed or skipped rows are real problems and keep alerting.
        self.assertEqual(result["status"], "partial")
        self.assertIn("log:L9:unknown-submitter", result["issues"])
        self.assertEqual(result["notices"], {})

    def test_source_read_failure_is_not_treated_as_everyone_missing(self):
        self.logs.get_logs_by_date.side_effect = RuntimeError("日志表读取失败")
        result = self.workflow.refresh_submission_statistics(DAY)
        self.assertEqual(result["status"], "failed")
        self.statistics.publish.assert_not_called()

    def test_existing_snapshot_roster_is_used_but_logs_are_read_fresh(self):
        self.store.get_or_create_workflow(DAY)
        self.store.save_snapshot(self.snapshot)
        self.logs.get_logs_by_date.return_value = ([], {})
        self.workflow.refresh_submission_statistics(DAY)
        self.organization.get.assert_not_called()
        self.assertEqual(self.statistics.publish.call_args.args[2], [])

    def test_statistics_failure_does_not_block_daily_evaluation(self):
        self.statistics.publish.side_effect = RuntimeError("统计表写入失败")
        self.workflow._process_logs = Mock(side_effect=RuntimeError("评价已启动"))
        self.workflow.start(DAY)
        self.workflow._process_logs.assert_called_once_with(DAY)

    def test_pending_submission_counts_without_flushing_or_starting_workflow(self):
        inbox = WorkBuddyInbox(self.store.root)
        submission = WorkBuddySubmission(submission_id="pending", person_id="M", name="成员",
            role="基层学生", submitted_at=self.snapshot.logs[0].submitted_at, target_date=DAY,
            progress="待归档的日志")
        path = inbox.save(submission)
        workbuddy = Mock(inbox=inbox)
        self.workflow.workbuddy = workbuddy
        self.logs.get_logs_by_date.return_value = ([], {})
        self.workflow.refresh_submission_statistics(DAY)
        logs = self.statistics.publish.call_args.args[2]
        self.assertEqual([log.person_id for log in logs], ["M"])
        self.assertTrue(path.exists())
        workbuddy.flush.assert_not_called()
        self.assertIsNone(self.store.load_workflow(DAY))


class ScheduledStatisticsTests(unittest.TestCase):
    def setUp(self):
        from config.schedules import load_schedule_config
        from service.orchestration import Orchestrator
        self.fixture = IndependentStatisticsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.workflow = self.fixture.workflow
        self.prompt = self.workflow.prompt_service
        binding = SimpleNamespace(workflow=self.workflow, auto_advance_at="21:50")
        runtime = SimpleNamespace(workflows={"workflow1_daily": binding}, workflow2=None,
                                  prompt_repository=None)
        settings = SimpleNamespace(state_dir=self.fixture.store.root, llm_concurrency=3,
            llm_retry_interval_minutes=60, llm_retry_limit=3, workflow2_enabled=False)
        self.orchestration = Orchestrator(runtime, load_schedule_config(), Mock(), settings)
        self.addCleanup(self.orchestration.tasks.close)
        self.orchestration.plan_day(DAY + timedelta(days=1))
        self.task = next(t for t in self.orchestration.tasks.journal.all()
                         if t["action"] == "daily_statistics")

    def test_statistics_retries_after_daily_report_has_completed(self):
        self.fixture.store.get_or_create_workflow(DAY)
        self.fixture.store.save_snapshot(self.fixture.snapshot)
        self.fixture.store.set_status(DAY, WorkflowStatus.COMPLETED)
        self.fixture.statistics.publish.side_effect = [RuntimeError("temporary outage"),
            {"status": "complete", "expected": 3, "written": 3, "issues": {}}]
        now = datetime.fromisoformat(self.task["due"]).astimezone(timezone.utc)
        tasks = self.orchestration.tasks
        with patch("service.execution.utc_now") as clock:
            clock.return_value = now
            tasks.execute(self.task["key"])
            retry = tasks.journal.get(self.task["key"] + "#retry:1")
            self.assertEqual(datetime.fromisoformat(retry["due"]), now + timedelta(hours=1))
            clock.return_value = now + timedelta(hours=1)
            tasks.execute(retry["key"])
        self.assertEqual(self.fixture.statistics.publish.call_count, 2)
        self.assertEqual(tasks.journal.get(retry["key"])["status"], "done")
        self.assertEqual(self.fixture.store.load_workflow(DAY).status, WorkflowStatus.COMPLETED)
        self.prompt.execute.assert_not_called()
        self.workflow.documents.advance.assert_not_called()
        self.workflow.reports.publish.assert_not_called()
        self.workflow.messages.send_text.assert_not_called()

    def test_incomplete_write_retries_but_source_warning_does_not(self):
        task = self.task
        self.workflow.refresh_submission_statistics = Mock(return_value={
            "status": "partial", "expected": 3, "written": 2, "issues": {"one": "write failed"}})
        with self.assertRaises(RuntimeError):
            self.orchestration.handle(task)
        self.workflow.refresh_submission_statistics.return_value = {
            "status": "partial", "expected": 3, "written": 3, "issues": {"one": "source warning"}}
        self.orchestration.handle(task)

    def test_daily_generation_does_not_repeat_independently_scheduled_statistics(self):
        self.workflow._process_logs = Mock(side_effect=RuntimeError("stop after snapshot"))
        self.workflow.start(DAY)
        self.fixture.statistics.publish.assert_not_called()
        self.workflow._process_logs.assert_called_once_with(DAY)


if __name__ == "__main__":
    unittest.main()

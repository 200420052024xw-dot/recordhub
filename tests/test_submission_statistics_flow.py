from __future__ import annotations

import tempfile
import unittest
from unittest.mock import Mock

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


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from unittest.mock import Mock

from test_workflow1_pipeline import DAY, sample_snapshot
from data import FileStateStore
from workflow1.models import DailySnapshot, UnitStatus
from workflow1.workflow import Workflow1


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = FileStateStore(directory.name, snapshot_model=DailySnapshot)
        self.store.get_or_create_workflow(DAY)
        snapshot = sample_snapshot()
        for state in snapshot.log_evaluations.values():
            state.status = UnitStatus.WAITING_CONFIRMATION
            state.positive_ai = state.positive_final = "AI positive"
            state.improvement_ai = state.improvement_final = "AI improvement"
        snapshot.evaluations_published = True
        self.store.save_snapshot(snapshot)
        human = Mock()
        human.for_date.return_value = []
        documents = Mock()
        documents.advance.return_value = {f"{DAY}:TEAM": Mock()}
        reports = Mock()
        reports.publish.return_value = {}
        self.workflow = Workflow1(store=self.store, organization_cache=Mock(),
            log_repository=Mock(), ai_evaluations=Mock(), human_evaluations=human,
            prompt_service=Mock(), messages=Mock(), documents=documents, reports=reports,
            auto_advance_at="21:50")
        self.workflow._deadline_passed = Mock(return_value=False)
        self.workflow.notify_documents_if_due = Mock()

    def test_no_report_before_cutoff_even_if_every_human_has_reviewed(self):
        def reviewed(snapshot):
            for state in snapshot.log_evaluations.values():
                state.status = UnitStatus.CONFIRMED
                state.source = "HUMAN"
        self.store.update_snapshot(DAY, reviewed)
        self.workflow.advance(DAY)
        self.assertEqual(self.workflow.finalize_pending_confirmations(DAY)["status"], "waiting")
        self.workflow.documents.advance.assert_not_called()
        self.workflow.prompt_service.execute.assert_not_called()

    def test_cutoff_keeps_ai_and_marks_missing_unit_without_calling_model(self):
        def missing(snapshot):
            state = snapshot.log_evaluations["L2"]
            state.status = UnitStatus.FAILED
            state.error = "provider unavailable"
        self.store.update_snapshot(DAY, missing)
        self.workflow._deadline_passed.return_value = True
        result = self.workflow.finalize_pending_confirmations(DAY)
        self.assertEqual(result["errors"], [])
        snapshot = self.store.load_snapshot(DAY)
        self.assertEqual(snapshot.log_evaluations["L1"].positive_final, "AI positive")
        self.assertIn("provider unavailable", snapshot.log_evaluations["L2"].positive_final)
        self.assertTrue(all(progress.closed for progress in snapshot.evaluators.values()))
        self.workflow.documents.advance.assert_called_once()
        self.workflow.prompt_service.execute.assert_not_called()

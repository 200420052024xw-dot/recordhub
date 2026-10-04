from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import UTC, date, datetime

HAS_PYDANTIC = importlib.util.find_spec("pydantic") is not None

if HAS_PYDANTIC:
    from schema import Department, Organization, Person, WorkLog
    from workflow1.models import DailySnapshot, UnitStatus
    from workflow1.snapshot import SnapshotBuilder
    from data import FileStateStore


@unittest.skipUnless(HAS_PYDANTIC, "workflow dependencies are not installed")
class WorkflowStateTests(unittest.TestCase):
    target_date = date(2026, 10, 3)

    def organization(self) -> "Organization":
        return Organization(
            persons=[
                Person(
                    person_id="P1",
                    name="学生",
                    role="基层学生",
                    department_id="D1",
                    leader_id="C1",
                ),
                Person(
                    person_id="C1",
                    name="骨干",
                    role="骨干学生",
                    department_id="D1",
                    leader_id="M1",
                ),
                Person(
                    person_id="M1",
                    name="部长",
                    role="部长",
                    department_id="D1",
                ),
                Person(person_id="T1", name="负责人", role="团队负责人"),
            ],
            departments=[
                Department(department_id="D1", name="一部", minister_id="M1")
            ],
            team_leader_id="T1",
        )

    def snapshot(self, run_id: str):
        return SnapshotBuilder().build(
            workflow_run_id=run_id,
            target_date=self.target_date,
            organization=self.organization(),
            logs=[
                WorkLog(
                    log_id="L1",
                    submitted_at=datetime(2026, 10, 3, 12, tzinfo=UTC),
                    person_id="P1",
                    progress="完成测试",
                )
            ],
            now=datetime(2026, 10, 4, 0, tzinfo=UTC),
        )

    def test_workflow_is_idempotent_and_survives_new_store_instance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FileStateStore(directory, snapshot_model=DailySnapshot)
            first = store.get_or_create_workflow(self.target_date)
            second = store.get_or_create_workflow(self.target_date)
            self.assertEqual(first.workflow_run_id, second.workflow_run_id)
            store.save_snapshot(self.snapshot(first.workflow_run_id))

            reloaded = FileStateStore(directory, snapshot_model=DailySnapshot).load_snapshot(self.target_date)
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.workflow_run_id, first.workflow_run_id)
            self.assertEqual(set(reloaded.evaluators), {"C1", "M1"})
            self.assertTrue(reloaded.submission_status["P1"].submitted)
            self.assertFalse(reloaded.submission_status["C1"].submitted)

    def test_evaluation_and_closure_are_saved_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FileStateStore(directory, snapshot_model=DailySnapshot)
            run = store.get_or_create_workflow(self.target_date)
            store.save_snapshot(self.snapshot(run.workflow_run_id))
            def finalize(snapshot):
                snapshot.log_evaluations["L1"].status = UnitStatus.CONFIRMED
                snapshot.evaluators["C1"].closed = True

            store.update_snapshot(self.target_date, finalize)
            self.assertEqual(
                store.load_snapshot(self.target_date).log_evaluations["L1"].status,
                UnitStatus.CONFIRMED,
            )
            self.assertTrue(store.load_snapshot(self.target_date).evaluators["C1"].closed)

    def test_multiple_days_use_independent_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FileStateStore(directory)
            first = store.get_or_create_workflow(date(2026, 10, 2))
            second = store.get_or_create_workflow(date(2026, 10, 3))
            self.assertNotEqual(first.workflow_run_id, second.workflow_run_id)
            self.assertEqual(len(store.list_incomplete_workflows()), 2)


if __name__ == "__main__":
    unittest.main()

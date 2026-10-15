"""Unfinished days: surfaced while there is still time, deleted once there is not.

cleanup_completed deliberately never touches a day that did not complete, so
without these two paths one failed run keeps its state file forever.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta

from data import FileStateStore

TODAY = date(2026, 10, 15)


class StaleIncompleteTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory
        self.store = FileStateStore(directory.name)

    def day(self, days_ago: int) -> date:
        return TODAY - timedelta(days=days_ago)

    def test_old_incomplete_day_is_listed(self) -> None:
        self.store.get_or_create_workflow(self.day(8))
        stale = self.store.list_stale_incomplete(3, today=TODAY)
        self.assertEqual([run.target_date for run in stale], [self.day(8)])

    def test_recent_incomplete_day_is_not_listed(self) -> None:
        self.store.get_or_create_workflow(self.day(1))
        self.assertEqual(self.store.list_stale_incomplete(3, today=TODAY), [])

    def test_day_exactly_at_the_threshold_is_kept(self) -> None:
        self.store.get_or_create_workflow(self.day(3))
        self.assertEqual(self.store.list_stale_incomplete(3, today=TODAY), [])

    def test_completed_day_is_never_listed(self) -> None:
        # Completed days belong to cleanup_completed, however old they are.
        self.store.get_or_create_workflow(self.day(30))
        self.store.set_status(self.day(30), "COMPLETED")
        self.assertEqual(self.store.list_stale_incomplete(3, today=TODAY), [])

    def test_purge_removes_only_old_incomplete_days(self) -> None:
        old_incomplete = self.day(8)
        old_completed = self.day(9)
        recent_incomplete = self.day(1)
        for day in (old_incomplete, old_completed, recent_incomplete):
            self.store.get_or_create_workflow(day)
        self.store.set_status(old_completed, "COMPLETED")

        purged = self.store.purge_stale_incomplete(7, today=TODAY)

        self.assertEqual([item["target_date"] for item in purged], [old_incomplete])
        self.assertIsNone(self.store.load_workflow(old_incomplete))
        self.assertIsNotNone(self.store.load_workflow(old_completed))
        self.assertIsNotNone(self.store.load_workflow(recent_incomplete))

    def test_purge_reports_what_it_severs(self) -> None:
        day = self.day(8)
        self.store.get_or_create_workflow(day)
        self.store.set_status(day, "FAILED", error="FeishuApiError: timeout")
        self.store.map_external_record(day, f"{day}:TEAM:DETAIL", "check_details", "rec1")
        self.store.reserve_notification(day, "daily_report")

        purged = self.store.purge_stale_incomplete(7, today=TODAY)

        # The caller alerts on these counts: losing the mapping is what makes a
        # later re-run write duplicate rows into the Feishu tables.
        self.assertEqual(len(purged), 1)
        self.assertEqual(purged[0]["status"], "FAILED")
        self.assertEqual(purged[0]["last_error"], "FeishuApiError: timeout")
        self.assertEqual(purged[0]["external_records"], 1)
        self.assertEqual(purged[0]["notifications"], 1)

    def test_purge_also_removes_the_checked_archive(self) -> None:
        day = self.day(8)
        self.store.get_or_create_workflow(day)
        # get_or_create_workflow resurrects a day from this archive, so a purge
        # that leaves it behind would undo itself on the next call.
        archived = self.store.checked_dir / f"{day.isoformat()}.json"
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_text(
            json.dumps({"schema_version": 1, "revision": 1, "run": {
                "workflow_run_id": "archived-run", "workflow_type": "workflow1",
                "target_date": day.isoformat(), "status": "FAILED",
                "current_stage": "FAILED", "started_at": "2026-10-07T01:00:00Z",
                "updated_at": "2026-10-07T01:00:00Z", "retry_count": 0,
                "last_error": None}, "snapshot": None,
                "external_records": {}, "notifications": {}}),
            encoding="utf-8")

        self.store.purge_stale_incomplete(7, today=TODAY)

        self.assertFalse(archived.exists())
        revived = self.store.get_or_create_workflow(day)
        self.assertNotEqual(revived.workflow_run_id, "archived-run")


if __name__ == "__main__":
    unittest.main()

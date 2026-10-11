from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from unittest.mock import Mock

from test_workflow1_pipeline import DAY, sample_snapshot
from data import FileStateStore
from schema import Person, TableConfig
from workflow1.submission_statistics import SubmissionStatisticsRepository


class ReportDetailsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = FileStateStore(self.directory.name)
        self.store.get_or_create_workflow(DAY)
        self.snapshot = sample_snapshot()
        self.bitable = Mock()
        self.rows = []
        self.bitable.list_records.side_effect = lambda _: list(self.rows)
        self.bitable.create_record.side_effect = self.create_record
        self.bitable.update_record.side_effect = self.update_record
        config = TableConfig.model_validate({"tables": {
            "check_details": {"table_id": "details", "fields": {
                "title": "文本", "reporter_ref": "报告人", "role": "角色",
                "reported_at": "报告时间", "submitted": "已交人数", "missing": "未交人数"}},
        }})
        self.repo = SubmissionStatisticsRepository(self.bitable, config, self.store)
        self.key = f"{DAY}:DEPARTMENT:dep"

    def create_record(self, table_id, fields):
        row = {"record_id": f"new{len(self.rows)}", "fields": dict(fields)}
        self.rows.append(row)
        return row

    def update_record(self, table_id, record_id, fields):
        next(row for row in self.rows if row["record_id"] == record_id)["fields"].update(fields)

    def publish(self):
        return self.repo.publish(self.snapshot.target_date, self.snapshot.organization, self.snapshot.logs)

    def row_for(self, key):
        mapped = self.repo.store.get_external_record(self.snapshot.target_date, key + ":DETAIL")
        return next(row for row in self.rows if row["record_id"] == mapped["record_id"])["fields"]

    def test_complete_roster_is_written_without_evaluation_or_documents(self):
        self.snapshot.organization.persons.append(Person(person_id="N", name="未交成员",
            role="基层学生", department_id="dep", leader_id="B"))
        self.assertFalse(self.snapshot.evaluations_published)
        self.assertFalse(self.snapshot.cloud_objects)
        result = self.publish()
        self.assertEqual((result["status"], result["expected"], result["written"]), ("complete", 3, 3))
        for key in (f"{DAY}:MEMBERS:B", self.key, f"{DAY}:TEAM"):
            fields = self.row_for(key)
            # Two logs by M still count as one person; missing OpenID on a
            # member must not remove that member from submitted/missing totals.
            self.assertEqual((fields["已交人数"], fields["未交人数"]), (2, 1))
            self.assertNotIn("文本", fields)
        self.assertEqual(len({row["fields"]["报告时间"] for row in self.rows}), 1)

    def test_empty_logs_and_empty_group_are_still_written(self):
        self.snapshot.logs = []
        self.snapshot.organization.persons.append(Person(person_id="B2", name="空组骨干",
            role="骨干学生", department_id="dep", leader_id="D", open_id="ou_b2"))
        result = self.publish()
        self.assertEqual(result["written"], 4)
        fields = self.row_for(f"{DAY}:MEMBERS:B2")
        self.assertEqual((fields["已交人数"], fields["未交人数"]), (0, 1))
        self.assertEqual(self.row_for(self.key)["未交人数"], 3)

    def test_unmapped_neighboring_records_are_not_adopted_or_updated(self):
        for offset in (-1, 0, 1):
            self.rows.append({"record_id": f"old{offset}", "fields": {
                "报告人": [{"id": "ou_d"}], "报告时间": (
                    self.snapshot.created_at + timedelta(days=offset)).isoformat(),
                "已交人数": 99, "未交人数": 99}})
        self.publish()
        self.repo.store = FileStateStore(self.directory.name, workflow_type="submission_statistics")
        self.publish()
        self.assertEqual(self.bitable.create_record.call_count, 3)
        self.bitable.update_record.assert_not_called()
        self.assertEqual(self.row_for(self.key)["已交人数"], 2)
        self.assertTrue(all(row["fields"]["已交人数"] == 99 for row in self.rows[:3]))

    def test_same_reporter_has_separate_records_for_each_scope_and_date(self):
        self.snapshot.organization.team_leader_id = "D"
        self.publish()
        first = self.repo.store.get_external_record(DAY, self.key + ":DETAIL")["record_id"]
        team = self.repo.store.get_external_record(DAY, f"{DAY}:TEAM:DETAIL")["record_id"]
        next_day = DAY + timedelta(days=1)
        self.snapshot.target_date = next_day
        self.store.get_or_create_workflow(next_day)
        self.publish()
        self.publish()
        next_id = self.repo.store.get_external_record(next_day, f"{next_day}:DEPARTMENT:dep:DETAIL")["record_id"]
        self.assertEqual(len({first, team, next_id}), 3)
        self.assertEqual(self.bitable.create_record.call_count, 6)

    def test_mapped_old_record_is_updated_even_when_report_time_is_outside_window(self):
        self.rows.append({"record_id": "mapped", "fields": {
            "报告人": [{"id": "ou_d"}], "报告时间": (
                self.snapshot.created_at - timedelta(days=10)).isoformat(),
            "已交人数": 0, "未交人数": 0}})
        self.store.map_external_record(DAY, self.key + ":DETAIL", "check_details", "mapped")
        self.publish()
        self.bitable.update_record.assert_called_once_with("details", "mapped", {"已交人数": 2})

    def test_missing_mapped_record_does_not_stop_other_reporters(self):
        self.store.map_external_record(DAY, self.key + ":DETAIL", "check_details", "deleted")
        result = self.publish()
        self.assertEqual((result["status"], result["written"]), ("partial", 2))
        self.assertIn("本地映射与飞书不一致", " ".join(result["issues"].values()))
        self.assertEqual(self.row_for(f"{DAY}:TEAM")["已交人数"], 2)

    def test_failed_write_is_isolated_and_retry_only_fills_missing_row(self):
        def fail_minister(table_id, fields):
            if fields["报告人"] == [{"id": "ou_d"}]:
                raise RuntimeError("飞书拒绝此行")
            return self.create_record(table_id, fields)
        self.bitable.create_record.side_effect = fail_minister
        first = self.publish()
        self.assertEqual((first["status"], first["written"]), ("partial", 2))
        self.assertIn("部长", " ".join(first["issues"].values()))
        self.bitable.create_record.side_effect = self.create_record
        second = self.publish()
        self.assertEqual(second["status"], "complete")
        self.assertEqual(len(self.rows), 3)

    def test_missing_reporter_open_id_is_named_without_omitting_other_scopes(self):
        self.snapshot.organization.person_map()["D"].open_id = None
        result = self.publish()
        self.assertEqual((result["status"], result["expected"], result["written"]), ("partial", 3, 2))
        self.assertIn("「部长」（D）", " ".join(result["issues"].values()))
        self.assertEqual(self.row_for(f"{DAY}:TEAM")["已交人数"], 2)

    def test_statistics_do_not_require_valid_evaluation_ownership(self):
        self.snapshot.organization.person_map()["M"].leader_id = None
        self.snapshot.organization.anomalies.append("成员没有评价人")
        result = self.publish()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.row_for(self.key)["已交人数"], 2)

    def test_other_day_logs_are_not_counted(self):
        for log in self.snapshot.logs:
            log.submitted_at += timedelta(days=1)
        self.publish()
        self.assertEqual(self.row_for(self.key)["已交人数"], 0)
        self.assertEqual(self.row_for(self.key)["未交人数"], 2)

    def test_statistics_identity_survives_daily_snapshot_cleanup(self):
        self.publish()
        self.store.set_status(DAY, "COMPLETED")
        self.store.cleanup_completed(1, today=DAY + timedelta(days=4))
        self.assertIsNone(self.store.load_workflow(DAY))
        self.publish()
        self.assertEqual(len(self.rows), 3)

    def test_inactive_people_are_not_counted_or_given_summary_rows(self):
        self.snapshot.organization.person_map()["B"].active = False
        result = self.publish()
        self.assertEqual(result["expected"], 2)
        self.assertEqual(self.row_for(self.key)["已交人数"], 1)


if __name__ == "__main__":
    unittest.main()

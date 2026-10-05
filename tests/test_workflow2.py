import tempfile
import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from config import load_schedule_config
from schema import TableConfig
from workflow2.models import CycleRun, DepartmentResult
from workflow2.store import Workflow2Store
from workflow2.tables import Workflow2Tables, date_millis
from workflow2.workflow import Workflow2, _write_date_millis


class FakeBitable:
    def __init__(self):
        self.records = []
        self.next_id = 1

    def list_fields(self, table_id):
        return [{"field_name": "任务编号", "type": 1, "is_primary": True},
                {"field_name": "日期", "type": 5}]

    def list_records(self, table_id):
        return self.records

    def create_record(self, table_id, fields):
        row = {"record_id": f"rec{self.next_id}", "fields": dict(fields)}
        self.next_id += 1
        self.records.append(row)
        return row

    def update_record(self, table_id, record_id, fields):
        next(row for row in self.records if row["record_id"] == record_id)["fields"].update(fields)


class Workflow2TableTests(unittest.TestCase):
    def setUp(self):
        self.bitable = FakeBitable()
        config = TableConfig.model_validate({"tables": {"stage_report": {
            "table_id": "tblStage", "fields": {
                "task_id": "任务编号", "analysis_date": "日期"}}}})
        self.tables = Workflow2Tables(self.bitable, config)

    def test_upsert_uses_writable_primary_task_id(self):
        self.tables.require(["stage_report"])
        first = self.tables.upsert("stage_report", "cycle:department", {"analysis_date": 123})
        second = self.tables.upsert("stage_report", "cycle:department", {"analysis_date": 456})
        self.assertEqual(first, second)
        self.assertEqual(len(self.bitable.records), 1)
        self.assertEqual(self.bitable.records[0]["fields"],
                         {"任务编号": "cycle:department", "日期": 456})

    def test_feishu_write_date_uses_shanghai_day(self):
        with patch("workflow2.workflow.utc_now",
                   return_value=datetime(2026, 10, 4, 17, tzinfo=timezone.utc)):
            self.assertEqual(_write_date_millis(), date_millis(date(2026, 10, 5)))

    def test_missing_text_task_id_blocks_startup(self):
        self.bitable.list_fields = lambda table_id: [
            {"field_name": "任务编号", "type": 20},
            {"field_name": "日期", "type": 5}]
        with self.assertRaises(ValueError):
            self.tables.require(["stage_report"])

    def test_cycle_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Workflow2Store(directory)
            now = datetime.now(timezone.utc)
            run = CycleRun(run_id="927065ce-76e3-4837-9a1c-dff6ce6604cb",
                           kind="stage", scheduled_date=date(2026, 10, 5),
                           start_date=date(2026, 10, 2), end_date=date(2026, 10, 4),
                           created_at=now, updated_at=now)
            store.save(run)
            self.assertEqual(store.load(run.run_id).run_id, run.run_id)
            self.assertEqual(len(store.pending()), 1)

    def test_activation_waits_for_three_complete_days_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            activation = date(2026, 10, 5)
            store = Workflow2Store(directory)
            self.assertEqual(store.activate(activation), activation)
            self.assertTrue(store.activation_path.name.endswith(".toml"))
            self.assertIn('activation_date = "2026-10-05"',
                          store.activation_path.read_text(encoding="utf-8"))
            restarted_store = Workflow2Store(directory)
            self.assertEqual(restarted_store.activate(date(2026, 10, 7)), activation)
            workflow = Workflow2.__new__(Workflow2)
            workflow.store = restarted_store
            workflow.schedules = load_schedule_config("config/schedules.toml")
            workflow.start = Mock(return_value="started")
            self.assertIsNone(workflow.scheduled("stage", date(2026, 10, 8)))
            self.assertEqual(workflow.scheduled("stage", date(2026, 10, 9)), "started")
            workflow.start.assert_called_once_with("stage", date(2026, 10, 9))
            self.assertIsNone(workflow.scheduled("stage", date(2026, 10, 10)))
            self.assertEqual(workflow.scheduled("stage", date(2026, 10, 12)), "started")
            self.assertEqual(workflow.scheduled("weekly", date(2026, 10, 12)), "started")
            self.assertEqual(workflow.scheduled("weekly", date(2026, 10, 19)), "started")
            self.assertEqual(workflow.scheduled("monthly", date(2026, 11, 1)), "started")
            self.assertEqual(workflow.scheduled("monthly", date(2026, 12, 1)), "started")

    def test_weekly_and_monthly_do_not_depend_on_activation(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = Workflow2.__new__(Workflow2)
            workflow.store = Workflow2Store(directory)
            workflow.schedules = load_schedule_config("config/schedules.toml")
            workflow.start = Mock(return_value="started")
            self.assertEqual(workflow.scheduled("weekly", date(2026, 10, 5)), "started")
            self.assertEqual(workflow.scheduled("monthly", date(2026, 11, 1)), "started")
            self.assertFalse(workflow.store.activation_path.exists())

    def test_confirmation_after_cutoff_uses_ai_draft(self):
        run = CycleRun(run_id="927065ce-76e3-4837-9a1c-dff6ce6604cb",
                       kind="stage", scheduled_date=date(2026, 1, 5),
                       start_date=date(2026, 1, 2), end_date=date(2026, 1, 4),
                       created_at=datetime.now(timezone.utc),
                       updated_at=datetime.now(timezone.utc))
        draft = DepartmentResult(department_id="D1", minister_id="M1",
                                 draft_text={"S04": "AI draft"})
        run.departments["D1"] = draft
        fields = {"task_id": "任务编号", "submitted_by": "填写人：",
                  "analysis_date": "日期", "decision": "阶段工作总结是否确认",
                  "edited": "阶段工作修改"}
        record = {"record_id": "rec1", "last_modified_time": 1767830400000,
                  "fields": {"填写人：": [{"id": "ou_m1"}],
                             "日期": date_millis(date(2026, 1, 4)),
                             "阶段工作总结是否确认": "需修改",
                             "阶段工作修改": "late edit"}}
        writes = []
        table = SimpleNamespace(table_id="tbl", fields=fields)
        workflow = Workflow2.__new__(Workflow2)
        workflow.tables = SimpleNamespace(table=lambda name: table,
            bitable=SimpleNamespace(list_records=lambda table_id: [record]),
            upsert=lambda *args: writes.append(args))
        workflow.store = SimpleNamespace(save=lambda state: None)
        workflow.schedules = SimpleNamespace(workflows={"s04_progress":
            SimpleNamespace(options={"confirmation_days": 1,
                                     "confirmation_time": "12:00"})})
        person = SimpleNamespace(name="minister", open_id="ou_m1",
                                 person_id="M1", source_record_id="recM")
        organization = SimpleNamespace(person_map=lambda: {"M1": person})
        workflow._confirmation(run, draft, organization)
        self.assertEqual(draft.final_text["S04"], "AI draft")
        self.assertEqual(draft.unconfirmed, ["S04"])
        self.assertEqual(len(writes), 1)

    def test_confirmation_matches_scheduled_day(self):
        run = CycleRun(run_id="927065ce-76e3-4837-9a1c-dff6ce6604cb",
                       kind="stage", scheduled_date=date(2026, 10, 5),
                       start_date=date(2026, 10, 2), end_date=date(2026, 10, 4),
                       created_at=datetime.now(timezone.utc),
                       updated_at=datetime.now(timezone.utc))
        draft = DepartmentResult(department_id="D1", minister_id="M1",
                                 draft_text={"S04": "AI draft"})
        run.departments["D1"] = draft
        fields = {"task_id": "任务编号", "submitted_by": "填写人：",
                  "analysis_date": "日期", "decision": "阶段工作总结是否确认",
                  "edited": "阶段工作修改"}
        record = {"record_id": "rec1", "fields": {
            "填写人：": [{"id": "ou_m1"}],
            "日期": date_millis(run.scheduled_date),
            "阶段工作总结是否确认": "确认无误"}}
        writes = []
        table = SimpleNamespace(table_id="tbl", fields=fields)
        workflow = Workflow2.__new__(Workflow2)
        workflow.tables = SimpleNamespace(
            table=lambda name: table,
            bitable=SimpleNamespace(list_records=lambda table_id: [record],
                                    update_record=lambda *args: None),
            upsert=lambda *args: writes.append(args))
        workflow.store = SimpleNamespace(save=lambda state: None)
        workflow.schedules = SimpleNamespace(workflows={"s04_progress":
            SimpleNamespace(options={"confirmation_days": 1,
                                     "confirmation_time": "12:00"})})
        person = SimpleNamespace(name="minister", open_id="ou_m1",
                                 person_id="M1", source_record_id="recM")
        organization = SimpleNamespace(person_map=lambda: {"M1": person})
        with patch("workflow2.workflow.utc_now",
                   return_value=datetime(2026, 10, 5, 9, tzinfo=timezone.utc)):
            workflow._confirmation(run, draft, organization)
        self.assertEqual(draft.final_text["S04"], "AI draft")
        self.assertEqual(draft.confirmation_record_id, "rec1")
        self.assertEqual(draft.unconfirmed, [])
        self.assertEqual(len(writes), 1)

    def test_confirmation_matches_next_day_before_cutoff(self):
        run = CycleRun(run_id="927065ce-76e3-4837-9a1c-dff6ce6604cb",
                       kind="stage", scheduled_date=date(2026, 10, 5),
                       start_date=date(2026, 10, 2), end_date=date(2026, 10, 4),
                       created_at=datetime.now(timezone.utc),
                       updated_at=datetime.now(timezone.utc))
        draft = DepartmentResult(department_id="D1", minister_id="M1",
                                 draft_text={"S04": "AI draft"})
        table = SimpleNamespace(table_id="tbl", fields={
            "task_id": "任务编号", "submitted_by": "填写人：", "analysis_date": "日期",
            "decision": "阶段工作总结是否确认", "edited": "阶段工作修改"})
        record = {"record_id": "rec1", "created_time": 1791248400000,
                  "fields": {"填写人：": [{"id": "ou_m1"}],
                             "日期": date_millis(date(2026, 10, 6)),
                             "阶段工作总结是否确认": "确认无误"}}
        writes = []
        workflow = Workflow2.__new__(Workflow2)
        workflow.tables = SimpleNamespace(
            table=lambda name: table,
            bitable=SimpleNamespace(list_records=lambda table_id: [record],
                                    update_record=lambda *args: None),
            upsert=lambda *args: writes.append(args))
        workflow.store = SimpleNamespace(save=lambda state: None)
        workflow.schedules = SimpleNamespace(workflows={"s04_progress":
            SimpleNamespace(options={"confirmation_days": 1,
                                     "confirmation_time": "12:00"})})
        person = SimpleNamespace(name="minister", open_id="ou_m1",
                                 person_id="M1", source_record_id="recM")
        organization = SimpleNamespace(person_map=lambda: {"M1": person})
        with patch("workflow2.workflow.utc_now",
                   return_value=datetime(2026, 10, 6, 1, tzinfo=timezone.utc)):
            workflow._confirmation(run, draft, organization)
        self.assertEqual(draft.final_text["S04"], "AI draft")
        self.assertEqual(draft.confirmation_record_id, "rec1")
        self.assertEqual(draft.unconfirmed, [])
        self.assertEqual(len(writes), 1)


if __name__ == "__main__":
    unittest.main()

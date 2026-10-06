import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
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

    def test_report_link_uses_actual_field_type(self):
        self.tables.config.tables["stage_report"].fields["progress"] = "阶段工作总结"
        self.bitable.list_fields = lambda table_id: [
            {"field_name": "阶段工作总结", "type": 15}]
        self.assertEqual(self.tables.url_value(
            "stage_report", "progress", "https://feishu.cn/docx/x", "阶段汇报"),
            {"text": "阶段汇报", "link": "https://feishu.cn/docx/x"})

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

    def test_stage_ai_date_is_next_day_for_confirmation_lookup(self):
        from schema import Department, Organization, Person

        day = date(2026, 10, 5)
        run = CycleRun(run_id="927065ce-76e3-4837-9a1c-dff6ce6604cb",
                       kind="stage", scheduled_date=day,
                       start_date=date(2026, 10, 2), end_date=date(2026, 10, 4),
                       created_at=datetime.now(timezone.utc),
                       updated_at=datetime.now(timezone.utc))
        run.departments["D"] = DepartmentResult(
            department_id="D", minister_id="M",
            drafts={"S04": {"content": {"items": []}}},
            draft_text={"S04": "AI 阶段分析"})
        organization = Organization(
            persons=[Person(person_id="M", name="部长", role="部长",
                            department_id="D", open_id="ou_m")],
            departments=[Department(department_id="D", name="一部",
                                    minister_id="M")])
        workflow = Workflow2.__new__(Workflow2)
        workflow.schedules = load_schedule_config("config/schedules.toml")
        workflow.store = SimpleNamespace(save=lambda state: None)
        workflow.configs = SimpleNamespace(load=lambda org: [])
        workflow.prompt_service = Mock()
        workflow.preparer = SimpleNamespace(prepare=lambda **kwargs: SimpleNamespace(
            input=SimpleNamespace(scope=SimpleNamespace(
                complete=True, omitted_sources=[]))))
        workflow.tables = SimpleNamespace(upsert=Mock())
        workflow._confirmation = Mock()
        workflow._notify_minister = Mock()
        workflow._departments(run, organization)
        table, _, fields = workflow.tables.upsert.call_args.args
        self.assertEqual(table, "stage_analysis")
        self.assertEqual(fields["analysis_date"], date_millis(day + timedelta(days=1)))

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
        self.assertEqual(len(writes), 0)

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
        self.assertEqual(len(writes), 0)

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
        self.assertEqual(len(writes), 0)

    def test_periodic_links_go_to_their_report_tables(self):
        from schema import Department, Organization, Person

        class FakeDocs:
            def __init__(self):
                self.created = {}

            def find_child(self, parent, title, kind):
                return self.created.get((parent, title, kind))

            def create_folder(self, parent, title):
                token = f"folder{len(self.created)}"
                self.created[(parent, title, "folder")] = token
                return token

            def create_document(self, parent, title):
                token = f"doc{len(self.created)}"
                self.created[(parent, title, "docx")] = token
                return token

            def list_blocks(self, token):
                return []

            def append_blocks(self, token, blocks):
                pass

        organization = Organization(
            persons=[Person(person_id="T", name="负责人", role="团队负责人",
                            open_id="ou_t"),
                     Person(person_id="M", name="部长", role="部长",
                            open_id="ou_m", department_id="D")],
            departments=[Department(department_id="D", name="一部", minister_id="M")],
            team_leader_id="T")
        for kind, codes, table_name in (
            ("stage", ("S04",), "stage_report"),
            ("monthly", ("S05", "S06", "S07"), "monthly_department_report"),
            ("weekly", ("S08", "S09"), "team_analysis"),
        ):
            with self.subTest(kind=kind):
                run = CycleRun(run_id="927065ce-76e3-4837-9a1c-dff6ce6604cb",
                               kind=kind, scheduled_date=date(2026, 10, 5),
                               start_date=date(2026, 10, 2), end_date=date(2026, 10, 4),
                               created_at=datetime.now(timezone.utc),
                               updated_at=datetime.now(timezone.utc))
                if kind == "weekly":
                    run.team_results = {code: {"content": {"items": []}}
                                        for code in codes}
                    run.weekly_reference = {code: {"content": {"items": []}}
                                            for code in codes}
                else:
                    run.departments["D"] = DepartmentResult(
                        department_id="D", minister_id="M",
                        final_text={code: f"{code} 正文" for code in codes})
                writes = []
                workflow = Workflow2.__new__(Workflow2)
                workflow.documents = FakeDocs()
                workflow.archive_parent = "root"
                workflow.store = SimpleNamespace(save=lambda state: None)
                workflow.schedules = load_schedule_config("config/schedules.toml")
                workflow.tables = SimpleNamespace(
                    upsert=lambda *args: writes.append(args),
                    url_value=lambda table, field, url, title: url)
                workflow._notify = Mock()
                workflow._documents(run, organization)
                self.assertEqual({entry[0] for entry in writes}, {table_name})
                if kind == "weekly":
                    self.assertEqual(len(writes), 1)
                    self.assertEqual(writes[0][1], run.run_id)
                    self.assertEqual(set(writes[0][2]),
                                     {"analysis_date", "technology_s08", "training_s09"})
                    for column in ("technology_s08", "training_s09"):
                        self.assertTrue(writes[0][2][column].startswith("https://feishu.cn/docx/"))
                    continue
                self.assertEqual(len(writes), 2)
                department_fields = next(entry[2] for entry in writes
                                         if entry[1].endswith(":D"))
                team_fields = next(entry[2] for entry in writes
                                   if entry[1].endswith(":TEAM"))
                columns = (["progress"] if kind == "stage" else
                           ["personnel_s05", "ideas_s06", "meeting_s07"])
                for column in columns:
                    self.assertTrue(department_fields[column].startswith("https://feishu.cn/docx/"))
                    self.assertTrue(team_fields[column].startswith("https://feishu.cn/docx/"))
                    self.assertNotEqual(department_fields[column], team_fields[column])


if __name__ == "__main__":
    unittest.main()

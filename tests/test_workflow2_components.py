"""Coverage for the retained Workflow2 material and Skill boundaries."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config import AppSettings, DeepSeekSettings, FeishuSettings
from data import FileStateStore
from llm import LLMError, PromptService
from service.runtime import build_runtime
from workflow1.models import DailySnapshot, UnitStatus
from workflow2.materials import MaterialPreparer
from workflow2.skills import (MaterialScope, SkillCode, SkillConfig, SkillInput,
                              SkillRunner, SkillStatus, TextRecord)
from test_workflow1_pipeline import DAY, sample_snapshot


class Workflow2ComponentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.daily = FileStateStore(self.temp.name, snapshot_model=DailySnapshot)
        self.daily.get_or_create_workflow(DAY)
        self.snapshot = sample_snapshot()
        self.snapshot.workflow_run_id = self.daily.load_workflow(DAY).workflow_run_id
        for state in self.snapshot.log_evaluations.values():
            state.status = UnitStatus.CONFIRMED
            state.positive_ai = state.positive_final = "完成检查"
            state.improvement_ai = state.improvement_final = "补齐材料"
        self.daily.save_snapshot(self.snapshot)

    def material(self):
        return SkillInput(run_id="skill-test",
            scope=MaterialScope(start_date=DAY, end_date=DAY, department_ids=["dep"]),
            records=[TextRecord(record_id="rec1", person_id="M", name="成员",
                department_id="dep", role="基层学生", work_start=DAY, work_end=DAY,
                confirmed=True, progress="完成检查")])

    def test_all_periodic_skills_load_external_prompts_and_validate_json(self):
        client = Mock()
        client.complete_json.side_effect = lambda messages, validator: validator({"items": []})
        for code in SkillCode:
            with self.subTest(code=code):
                result = SkillRunner(PromptService(client, max_attempts=1)).run(
                    code, self.material(), department_id="dep", user_id="D")
                self.assertEqual(result.status, SkillStatus.WAITING_CONFIRMATION)
                template = client.complete_json.call_args.args[0][0]["content"]
                self.assertIn((ROOT / "prompts" / f"{code.value}.txt")
                              .read_text(encoding="utf-8").strip(), template)

    def test_skill_rejects_unknown_participant(self):
        client = Mock()
        client.complete_json.side_effect = lambda messages, validator: validator({"items": [{
            "work_item": "工作", "participant_ids": ["ghost"],
            "current_progress": "完成"}]})
        with self.assertRaises(LLMError):
            SkillRunner(PromptService(client, max_attempts=1)).run(
                "S04", self.material(), department_id="dep", user_id="D")

    def test_only_finalized_evaluation_reaches_material(self):
        prepared = MaterialPreparer(self.daily).prepare(
            run_id="r1", start=DAY, end=DAY, department_ids=["dep"])
        submitted = [record for record in prepared.input.records if record.submitted]
        self.assertTrue(submitted)
        for record in submitted:
            self.assertEqual(len(record.evaluations), 1)
            self.assertEqual(record.evaluations[0].source, "AI")
        snapshot = self.daily.load_snapshot(DAY)
        for state in snapshot.log_evaluations.values():
            state.source = "HUMAN"
            state.positive_final = "老师肯定"
            state.improvement_final = "老师改进"
        self.daily.save_snapshot(snapshot)
        prepared = MaterialPreparer(self.daily).prepare(
            run_id="r2", start=DAY, end=DAY, department_ids=["dep"])
        for record in prepared.input.records:
            if record.submitted:
                self.assertEqual(len(record.evaluations), 1)
                self.assertEqual(record.evaluations[0].source, "HUMAN")
                self.assertEqual(record.evaluations[0].positive, "老师肯定")

    def test_person_filter_restricts_groups(self):
        full = MaterialPreparer(self.daily).prepare(
            run_id="r1", start=DAY, end=DAY, department_ids=["dep"])
        submitted_ids = {record.person_id for record in full.input.records if record.submitted}
        self.assertGreater(len(submitted_ids), 1)
        one = sorted(submitted_ids)[0]
        grouped = MaterialPreparer(self.daily).prepare(
            run_id="r2", start=DAY, end=DAY, department_ids=["dep"], person_ids=[one])
        self.assertEqual({record.person_id for record in grouped.input.records
                          if record.submitted}, {one})

    def test_partial_period_uses_available_day_and_reports_omissions(self):
        prepared = MaterialPreparer(self.daily).prepare(
            run_id="partial", start=DAY - timedelta(days=1), end=DAY,
            department_ids=["dep"], allow_partial=True)
        self.assertTrue(prepared.input.scope.complete)
        self.assertEqual(prepared.input.scope.missing_sources, [])
        self.assertTrue(any("每日检查快照缺失" in source
                            for source in prepared.input.scope.omitted_sources))
        self.assertTrue(any(record.submitted for record in prepared.input.records))

    def test_personal_skill_config_precedes_department_default(self):
        department = SkillConfig(config_id="dept", skill_code="S04",
                                 department_id="dep", version="1")
        personal = department.model_copy(update={"config_id": "person", "user_id": "D"})
        runner = SkillRunner(PromptService(Mock()), [department, personal])
        self.assertEqual(runner.resolve_config("S04", department_id="dep",
                                               user_id="D").config_id, "person")

    def test_completed_daily_snapshot_deleted_after_retention(self):
        self.daily.set_status(DAY, "COMPLETED")
        self.daily.cleanup_completed(2, today=date(2026, 10, 10))
        self.assertFalse((self.daily.runs_dir / f"{DAY}.json").exists())
        self.assertFalse((self.daily.checked_dir / f"{DAY}.json").exists())
        prepared = MaterialPreparer(self.daily).prepare(
            run_id="range", start=DAY, end=DAY, department_ids=["dep"])
        self.assertFalse(prepared.input.scope.complete)
        self.assertTrue(any("每日检查快照缺失" in item
                            for item in prepared.input.scope.missing_sources))

    def test_runtime_reuses_workflow1_store_for_workflow2_materials(self):
        settings = AppSettings(feishu=FeishuSettings("app", "secret", "base"),
            deepseek=DeepSeekSettings("key"),
            state_dir=self.temp.name, archive_parent_folder_token="folder",
            table_config_path=str(ROOT / "config/tables.toml"),
            schedule_config_path=str(ROOT / "config/schedules.toml"),
            workflow2_enabled=True)
        with patch("service.runtime._build_infrastructure", return_value=Mock()):
            runtime = build_runtime(settings)
        self.assertIs(runtime.workflow2.preparer.store,
                      runtime.workflows["workflow1_daily"].store)


class SnapshotRebuilderTests(unittest.TestCase):
    """Feishu-side reconstruction feeds MaterialPreparer once local caches die."""

    def setUp(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from data.repositories import LogRepository
        from schema import Department, Organization, Person, TableConfig
        from workflow2.feishu_material import SnapshotRebuilder

        self.datetime, self.ZoneInfo = datetime, ZoneInfo
        organization = Organization(persons=[
            Person(person_id="T", name="部长", role="部长", department_id="dep",
                   open_id="ou_t"),
            Person(person_id="K", name="骨干", role="骨干学生", department_id="dep",
                   leader_id="T", open_id="ou_k"),
            Person(person_id="M", name="成员", role="基层学生", department_id="dep",
                   leader_id="K", open_id="ou_m"),
        ], departments=[Department(department_id="dep", name="部门", minister_id="T")],
            team_leader_id="T")
        self.cache = SimpleNamespace(get=lambda: organization)
        self.tables = TableConfig.model_validate({"tables": {
            "logs": {"table_id": "tblLogs", "fields": {
                "log_id": "日志编号", "submitted_at": "提交时间",
                "submitter_ref": "提交人", "progress": "工作进展",
                "difficulties": "困难", "reflection": "反思", "other": "其他",
                "full_log": "合成日志"}},
            "evaluations": {"table_id": "tblAi", "fields": {
                "evaluation_id": "评价编号", "business_key": "业务编号",
                "person_ref": "被评价人", "evaluator_ref": "评价人",
                "source_log": "工作日志", "evaluated_at": "评价时间",
                "positive_ai": "肯定之处_AI", "improvement_ai": "改进之处_AI"}},
            "human_evaluations": {"table_id": "tblHuman", "fields": {
                "evaluation_id": "评价编号", "person_ref": "被评价人",
                "evaluator_ref": "评价人", "evaluated_at": "评价时间",
                "positive_final": "肯定之处_人工", "improvement_final": "需改进之处_人工",
                "submitted_by": "填写人", "positive_confirmed": "肯定之处_AI是否确认",
                "improvement_confirmed": "需改进之处_AI是否确认："}},
        }})
        self.rows = {"tblLogs": [{
            "record_id": "recL1", "fields": {
                "日志编号": "L1", "提交时间": self._millis(10),
                "提交人": [{"id": "ou_m"}], "工作进展": "完成检查",
                "困难": "", "反思": "", "其他": "", "合成日志": ""}}],
            "tblAi": [{
            "record_id": "recA1", "fields": {
                "评价编号": self.key, "业务编号": self.key,
                "被评价人": [{"id": "ou_m"}], "评价人": [{"id": "ou_k"}],
                "工作日志": "工作进展:完成检查\n工作困难:未填写\n心得反思:未填写\n其他:未填写",
                "评价时间": self._millis(11),
                "肯定之处_AI": "AI肯定", "改进之处_AI": "AI改进"}}],
            "tblHuman": []}
        bitable = SimpleNamespace(
            list_records=lambda table_id, field_names=None: self.rows.get(table_id, []))
        log_repository = LogRepository(bitable, self.tables, self.cache)
        self.rebuilder = SnapshotRebuilder(bitable, self.tables, self.cache,
                                           log_repository, auto_advance_at="12:00")

    @property
    def key(self):
        return f"{DAY.isoformat()}:EVAL:K:L1"

    def _millis(self, hour):
        moment = self.datetime(DAY.year, DAY.month, DAY.day, hour,
                                tzinfo=self.ZoneInfo("Asia/Shanghai"))
        return int(moment.timestamp() * 1000)

    def test_auto_passed_day_finalizes_with_ai_text(self):
        snapshot = self.rebuilder.rebuild(DAY, DAY)[DAY]
        state = snapshot.log_evaluations["L1"]
        self.assertEqual(state.status, UnitStatus.CONFIRMED)
        self.assertEqual(state.source, "AI")
        self.assertEqual(state.positive_final, "AI肯定")
        self.assertEqual(state.improvement_final, "AI改进")
        self.assertTrue(snapshot.submission_status["M"].submitted)
        self.assertFalse(snapshot.submission_status["K"].submitted)

    def test_human_row_finalizes_per_confirmation_flags(self):
        self.rows["tblHuman"].append({"record_id": "recH1", "fields": {
            "评价编号": self.key, "被评价人": [{"id": "ou_m"}],
            "评价人": [{"id": "ou_k"}], "填写人": [{"id": "ou_k"}],
            "评价时间": self._millis(12),
            "肯定之处_AI是否确认": "确认无误", "需改进之处_AI是否确认：": "需修改",
            "肯定之处_人工": "", "需改进之处_人工": "老师改进"}})
        state = self.rebuilder.rebuild(DAY, DAY)[DAY].log_evaluations["L1"]
        self.assertEqual(state.status, UnitStatus.CONFIRMED)
        self.assertEqual(state.source, "HUMAN")
        self.assertEqual(state.positive_final, "AI肯定")
        self.assertEqual(state.improvement_final, "老师改进")

    def test_rebuilt_snapshot_passes_material_preparer(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FileStateStore(directory, snapshot_model=DailySnapshot)
            preparer = MaterialPreparer(store, rebuilder=self.rebuilder)
            prepared = preparer.prepare(run_id="weekly", start=DAY, end=DAY,
                                        department_ids=["dep"], allow_rebuild=True)
        self.assertTrue(prepared.input.scope.complete, prepared.input.scope.missing_sources)
        record = next(item for item in prepared.input.records if item.person_id == "M")
        self.assertTrue(record.confirmed)
        self.assertEqual([evaluation.source for evaluation in record.evaluations], ["AI"])

    def test_missing_ai_row_stays_reported_as_incomplete(self):
        self.rows["tblAi"] = [  # 未填写 marker rows must never become evaluations
            {"record_id": "recX", "fields": {
                "评价编号": f"{DAY.isoformat()}:MISSING:M", "业务编号": f"{DAY.isoformat()}:MISSING:M",
                "被评价人": [{"id": "ou_m"}], "评价人": [], "工作日志": "",
                "评价时间": self._millis(11),
                "肯定之处_AI": "未填写日志", "改进之处_AI": "未填写日志"}}]
        snapshot = self.rebuilder.rebuild(DAY, DAY)[DAY]
        self.assertEqual(snapshot.log_evaluations["L1"].status,
                         UnitStatus.WAITING_CONFIRMATION)


if __name__ == "__main__":
    unittest.main()

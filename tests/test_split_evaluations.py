from __future__ import annotations

import tempfile
import sys
import tomllib
import unittest
import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from config import AppSettings, DeepSeekSettings, FeishuSettings, load_table_config
from schema import Department, Organization, Person, TableConfig, WorkLog
from service.runtime import build_runtime
from workflow1.evaluations import HumanEvaluationRepository
from workflow1.models import DailySnapshot, LogEvaluationState, UnitStatus
from workflow2.feishu_material import SnapshotRebuilder

DAY = date(2026, 10, 4)


class SplitEvaluationTests(unittest.TestCase):
    def setUp(self):
        raw = tomllib.loads((ROOT / "config/tables.example.toml").read_text(encoding="utf-8"))
        for name, table in raw["tables"].items():
            table["table_id"] = name
        self.config = TableConfig.model_validate(raw)
        people = [
            Person(person_id="T", name="部长", role="部长", open_id="ou_t"),
            Person(person_id="B", name="骨干", role="骨干学生", open_id="ou_b", leader_id="T"),
            Person(person_id="S", name="基层", role="基层学生", leader_id="B"),
        ]
        organization = Organization(persons=people, departments=[
            Department(department_id="D", name="部门", minister_id="T")])
        now = datetime(2026, 10, 5, 2, tzinfo=UTC)
        states = {log_id: LogEvaluationState(
            log_id=log_id, person_id=person_id, evaluator_id=evaluator_id,
            evaluation_id=f"{DAY}:EVAL:{evaluator_id}:{log_id}",
            positive_ai="AI肯定", improvement_ai="AI建议",
            status=UnitStatus.WAITING_CONFIRMATION)
            for log_id, person_id, evaluator_id in [("LS", "S", "B"), ("LB", "B", "T")]}
        self.snapshot = DailySnapshot(
            workflow_run_id="run", target_date=DAY, organization=organization,
            logs=[WorkLog(log_id=state.log_id, person_id=state.person_id,
                          submitted_at=now, full_log=state.log_id) for state in states.values()],
            submission_status={}, log_evaluations=states, created_at=now, updated_at=now)
        # The two forms have independent auto-number sequences: both may be PJ-1.
        common = {"评价编号": "PJ-1", "评价时间": "2026-10-05T09:00:00+08:00",
                  "肯定之处_AI是否确认": "确认无误",
                  "需改进之处_AI是否确认：": "需修改"}
        self.rows = {
            "human_evaluations": [{"record_id": "minister1", "fields": {
                **common, "被审核骨干：": [{"id": "ou_b"}],
                "填写人": [{"id": "ou_t"}], "改进之处_人工": "部长建议"}}],
            "human_evaluations_backbone": [{"record_id": "backbone1", "fields": {
                **common, "被审核基层：": "基层",
                "填写人": [{"id": "ou_b"}], "改进之处_人工": "骨干建议"}}],
        }
        self.bitable = Mock()
        self.bitable.list_records.side_effect = lambda table_id: self.rows.get(table_id, [])
        self.bitable.get_record.side_effect = lambda table_id, record_id: next(
            row for row in self.rows[table_id] if row["record_id"] == record_id)
        self.repo = HumanEvaluationRepository(self.bitable, self.config)

    def test_both_forms_match_despite_colliding_auto_numbers(self):
        parsed = [self.repo.parse(self.snapshot, row) for row in self.repo.for_date(self.snapshot)]
        self.assertEqual({value[1].person_id: value[1].improvement for value in parsed},
                         {"B": "部长建议", "S": "骨干建议"})
        self.assertTrue(all(value[1].positive == "AI肯定" for value in parsed))

    def test_event_record_is_loaded_from_its_source_table(self):
        row = self.repo.load_record("backbone1", table_id="human_evaluations_backbone")
        self.assertEqual(self.repo.parse(self.snapshot, row)[1].person_id, "S")
        self.bitable.get_record.assert_called_once_with("human_evaluations_backbone", "backbone1")
        with self.assertRaisesRegex(ValueError, "ID"):
            self.repo.load_record("backbone1", table_id="unrelated")

    def test_wrong_reviewer_and_late_form_are_ignored(self):
        row = self.rows["human_evaluations_backbone"][0]
        row["fields"]["填写人"] = [{"id": "ou_t"}]
        self.assertIsNone(self.repo.parse(self.snapshot, {**row, "_table_id": "human_evaluations_backbone"}))
        row["fields"]["评价时间"] = "2026-10-05T19:00:00+08:00"
        self.assertEqual(len(self.repo.for_date(self.snapshot)), 1)

    def test_period_material_rebuild_reads_both_forms(self):
        rebuild = SnapshotRebuilder(self.bitable, self.config,
            SimpleNamespace(get=lambda: self.snapshot.organization), Mock())
        ai_rows = {(DAY, state.log_id): {
            "person_id": state.person_id, "evaluator_id": state.evaluator_id,
            "source_log": state.log_id, "positive": state.positive_ai,
            "improvement": state.improvement_ai}
            for state in self.snapshot.log_evaluations.values()}
        rows = rebuild._read_human_rows(self.snapshot.organization, ai_rows)
        self.assertEqual(set(rows), {(DAY, "LS"), (DAY, "LB")})
        self.assertEqual(rebuild._finalize(rows[(DAY, "LS")], ai_rows[(DAY, "LS")])[:2],
                         ("AI肯定", "骨干建议"))

    def test_runtime_registers_both_event_handlers_with_table_id(self):
        settings = AppSettings(
            feishu=FeishuSettings("app", "secret", "base"), deepseek=DeepSeekSettings("key"),
            archive_parent_folder_token="archive",
            schedule_config_path=str(ROOT / "config/schedules.toml"))
        with tempfile.TemporaryDirectory() as directory, patch(
                "service.runtime.load_table_config", return_value=self.config), patch(
                "service.runtime._build_infrastructure", return_value=Mock()):
            from dataclasses import replace
            runtime = build_runtime(replace(settings, state_dir=directory))
            binding = runtime.workflows["workflow1_daily"]
            self.assertEqual(set(binding.record_handlers), set(self.rows))
            with patch.object(binding.workflow.human_evaluations, "load_record", return_value={}) as load:
                binding.record_handlers["human_evaluations_backbone"]("backbone1")
                load.assert_called_once_with("backbone1", table_id="human_evaluations_backbone")

    def test_missing_split_subject_mapping_fails_config_check(self):
        source = (ROOT / "config/tables.example.toml").read_text(encoding="utf-8")
        source = source.replace('table_id = ""', 'table_id = "test"')
        source = source.replace('basic_name = "被审核基层："', 'basic_name = ""')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tables.toml"
            path.write_text(source, encoding="utf-8")
            with self.assertRaises(ValueError):
                load_table_config(path)

    def test_example_mappings_match_exported_workbook_headers(self):
        sheets = {"departments": "部门表", "persons": "人员表", "logs": "工作日志表",
            "evaluations": "AI评价表", "human_evaluations": "人工评价表_部长",
            "human_evaluations_backbone": "人工评价表_骨干", "prompts": "skill配置表",
            "reports": "报告日志表", "check_details": "提交详情表",
            "stage_analysis": "阶段工作进展表", "stage_confirmation": "阶段工作确认表",
            "stage_report": "报告阶段工作表", "monthly_department_analysis": "每月部门分析表",
            "monthly_department_confirmation": "部门分析确认表",
            "monthly_department_report": "报告部门分析表", "team_analysis": "每月团队分析表"}
        # Only workbook headers are committed; student records stay local.
        headers = json.loads((ROOT / "tests/fixtures/bitable_headers.json").read_text(encoding="utf-8"))
        for name, sheet in sheets.items():
            with self.subTest(table=name):
                self.assertLessEqual(set(self.config.tables[name].fields.values()), set(headers[sheet]))


if __name__ == "__main__":
    unittest.main()

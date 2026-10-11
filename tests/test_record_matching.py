"""Regressions for normal records being mistaken for other business units."""
from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_workflow1_pipeline import DAY, sample_snapshot
from data import FileStateStore
from schema import CloudObject, TableConfig
from workflow1.evaluations import AiEvaluationRepository, HumanEvaluationRepository, evaluation_key
from workflow1.models import UnitStatus
from workflow1.reports import WorkflowReportRepository
from workflow2.models import CycleRun, DepartmentResult
from workflow2.tables import date_millis
from workflow2.workflow import Workflow2


class EvaluationMatchingTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = sample_snapshot()
        for state in self.snapshot.log_evaluations.values():
            state.status = UnitStatus.WAITING_CONFIRMATION
            state.positive_ai = "肯定"
            state.improvement_ai = "建议"
        self.fields = {
            "evaluation_id": "编号", "business_key": "编号", "person_ref": "被评价人",
            "source_log": "原文", "evaluated_at": "时间", "positive_ai": "肯定",
            "improvement_ai": "建议"}
        self.bitable = Mock()
        self.rows = []
        self.bitable.list_records.side_effect = lambda _: self.rows
        self.bitable.batch_create.side_effect = lambda _, fields: [
            {"record_id": f"new{i}", "fields": row} for i, row in enumerate(fields)]
        config = TableConfig.model_validate({"tables": {"evaluations": {
            "table_id": "ai", "fields": self.fields}}})
        self.repo = AiEvaluationRepository(self.bitable, config, Mock())

    def test_equal_content_from_another_day_person_or_log_is_not_reclaimed(self):
        for key, person in [(f"{DAY - timedelta(days=1)}:EVAL:B:L1", "ou_m"),
                            (f"{DAY}:EVAL:D:another", "ou_b"),
                            (f"{DAY}:EVAL:B:another", "ou_m")]:
            with self.subTest(key=key):
                self.rows[:] = [{"record_id": "old", "fields": {
                    "编号": key, "被评价人": [{"id": person}],
                    "原文": self.snapshot.logs[0].content()}}]
                mapped, _ = self.repo.publish(self.snapshot)
                self.assertNotIn("old", {value[0] for value in mapped.values()})
                self.bitable.update_record.assert_not_called()

    def test_exact_business_key_reuses_row_and_duplicate_key_is_rejected(self):
        key = evaluation_key(DAY, "B", "L1")
        self.rows[:] = [{"record_id": "exact", "fields": {
            "编号": key, "原文": self.snapshot.logs[0].content()}}]
        mapped, _ = self.repo.publish(self.snapshot)
        self.assertEqual(mapped["L1"], ("exact", key))
        self.rows.append({"record_id": "duplicate", "fields": {"编号": key}})
        with self.assertRaisesRegex(ValueError, "业务编号.*重复记录"):
            self.repo.publish(self.snapshot)

    def human_repository(self):
        config = TableConfig.model_validate({"tables": {"human_evaluations": {
            "table_id": "human", "fields": {
                "evaluation_id": "编号", "person_ref": "人", "submitted_by": "填写人",
                "source_log": "原文", "positive_final": "肯定", "improvement_final": "建议"}}}})
        return HumanEvaluationRepository(self.bitable, config)

    def test_human_auto_number_uses_original_to_distinguish_same_day_logs(self):
        row = {"fields": {"编号": "PJ-1", "人": [{"id": "ou_m"}],
                          "填写人": [{"id": "ou_b"}], "肯定": "肯定", "建议": "建议",
                          "原文": self.snapshot.logs[1].content()}}
        result = self.human_repository().parse(self.snapshot, row)
        self.assertEqual(result[1].log_id, "L2")
        row["fields"]["原文"] = "其他日志"
        self.assertIsNone(self.human_repository().parse(self.snapshot, row))

    def test_human_unknown_explicit_business_key_does_not_fall_back(self):
        row = {"fields": {"编号": f"{DAY - timedelta(days=1)}:EVAL:D:L3",
                          "人": [{"id": "ou_b"}], "填写人": [{"id": "ou_d"}],
                          "肯定": "肯定", "建议": "建议"}}
        self.assertIsNone(self.human_repository().parse(self.snapshot, row))


class ReportLinkMatchingTests(unittest.TestCase):
    def test_url_prefix_is_not_a_match_in_link_or_legacy_text_column(self):
        for link_field in (True, False):
            with self.subTest(link_field=link_field), tempfile.TemporaryDirectory() as directory:
                fields = {"title": "文本", "reporter_ref": "人", "role": "角色", "reported_at": "时间"}
                if link_field:
                    fields["document_url"] = "链接"
                config = TableConfig.model_validate({"tables": {"reports": {
                    "table_id": "reports", "fields": fields}}})
                bitable = Mock()
                bitable.list_fields.return_value = [{"field_name": "链接", "type": 15}]
                bitable.list_records.return_value = [{"record_id": "old", "fields": {
                    "链接" if link_field else "文本": "旧报告\nhttps://feishu.cn/docx/abcdef"}}]
                bitable.batch_create.return_value = [{"record_id": "new"}]
                store = FileStateStore(directory)
                store.get_or_create_workflow(DAY)
                repo = WorkflowReportRepository(bitable, config, store)
                item = CloudObject(business_key=f"{DAY}:TEAM", token="abc", url="https://feishu.cn/docx/abc")
                repo.publish(sample_snapshot(), {item.business_key: item})
                self.assertEqual(store.get_external_record(DAY, item.business_key)["record_id"], "new")

    def test_unrelated_duplicate_urls_do_not_block_a_new_report(self):
        with tempfile.TemporaryDirectory() as directory:
            config = TableConfig.model_validate({"tables": {"reports": {
                "table_id": "reports", "fields": {"title": "文本", "reporter_ref": "人",
                    "role": "角色", "reported_at": "时间"}}}})
            bitable = Mock()
            bitable.list_records.return_value = [{"record_id": name, "fields": {
                "文本": "旧报告\nhttps://feishu.cn/docx/old"}} for name in ("old1", "old2")]
            bitable.batch_create.return_value = [{"record_id": "new"}]
            store = FileStateStore(directory)
            store.get_or_create_workflow(DAY)
            item = CloudObject(business_key=f"{DAY}:TEAM", token="new", url="https://feishu.cn/docx/new")
            WorkflowReportRepository(bitable, config, store).publish(sample_snapshot(), {item.business_key: item})
            bitable.batch_create.assert_called_once()


class CycleConfirmationMatchingTests(unittest.TestCase):
    def setUp(self):
        self.run = CycleRun(run_id="927065ce-76e3-4837-9a1c-dff6ce6604cb", kind="stage",
            scheduled_date=DAY, start_date=DAY - timedelta(days=3), end_date=DAY - timedelta(days=1),
            created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
        self.draft = DepartmentResult(department_id="dep", minister_id="D", draft_text={"S04": "AI"})
        self.run.departments["dep"] = self.draft
        self.workflow = Workflow2.__new__(Workflow2)
        self.rows = []
        bitable = Mock()
        bitable.list_records.side_effect = lambda _: self.rows
        table = SimpleNamespace(table_id="confirm", fields={"task_id": "编号", "submitted_by": "填写人",
            "analysis_date": "日期", "decision": "选择", "edited": "修改"})
        self.workflow.tables = SimpleNamespace(table=lambda _: table, bitable=bitable)
        self.workflow.store = Mock()
        self.workflow.schedules = SimpleNamespace(workflows={"s04_progress": SimpleNamespace(
            options={"confirmation_days": 1, "confirmation_time": "12:00"})})

    def confirm(self):
        with patch("workflow2.workflow.utc_now", return_value=datetime(2026, 10, 3, 1, tzinfo=UTC)):
            self.workflow._confirmation(self.run, self.draft, sample_snapshot().organization)

    def test_exact_task_takes_precedence_over_nearby_unassigned_form(self):
        fields = {"填写人": [{"id": "ou_d"}], "日期": date_millis(DAY), "选择": "需修改"}
        self.rows.extend([
            {"record_id": "nearby", "fields": {**fields, "修改": "其他表单"}},
            {"record_id": "exact", "fields": {**fields, "编号": f"{self.run.run_id}:dep", "修改": "准确表单"}},
        ])
        self.confirm()
        self.assertEqual(self.draft.confirmation_record_id, "exact")
        self.assertEqual(self.draft.final_text["S04"], "准确表单")

    def test_unassigned_form_is_not_shared_between_departments_of_same_minister(self):
        self.run.departments["dep2"] = DepartmentResult(department_id="dep2", minister_id="D")
        self.rows.append({"record_id": "ambiguous", "fields": {"填写人": [{"id": "ou_d"}],
            "日期": date_millis(DAY), "选择": "需修改", "修改": "不知道哪个部门"}})
        self.confirm()
        self.assertIsNone(self.draft.confirmation_record_id)
        self.assertEqual(self.draft.final_text, {})
        self.workflow.tables.bitable.update_record.assert_not_called()


if __name__ == "__main__":
    unittest.main()

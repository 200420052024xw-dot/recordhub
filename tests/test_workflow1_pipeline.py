from __future__ import annotations

import tempfile
import sys
from urllib.parse import quote
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from data import FileStateStore
from workflow1.snapshot import SnapshotBuilder
from schema import CloudObject, Department, Organization, Person, TableConfig, WorkLog
from workflow1.models import DailySnapshot, UnitStatus
from workflow1.documents import DailyDocuments
from workflow1.evaluations import (AiEvaluationRepository,
                                        HumanEvaluationRepository, evaluation_key)
from workflow1.workflow import Workflow1
from workflow1.reports import WorkflowReportRepository
from workflow1.models import LogPromptOutput, WorkflowStatus

DAY = date(2026, 10, 3)


def sample_snapshot():
    organization = Organization(
        persons=[
            Person(person_id="T", name="负责人", role="团队负责人", open_id="ou_t"),
            Person(person_id="D", name="部长", role="部长", department_id="dep",
                   leader_id="T", open_id="ou_d"),
            Person(person_id="B", name="骨干", role="骨干学生",
                   department_id="dep", leader_id="D", open_id="ou_b"),
            Person(person_id="M", name="成员", role="基层学生",
                   department_id="dep", leader_id="B", open_id="ou_m"),
        ],
        departments=[Department(department_id="dep", name="一部", minister_id="D")],
        team_leader_id="T",
    )
    logs = [
        WorkLog(log_id="L1", source_record_id="rec1", person_id="M",
                submitted_at=datetime(2026, 10, 3, 8, tzinfo=UTC), full_log="第一条"),
        WorkLog(log_id="L2", source_record_id="rec2", person_id="M",
                submitted_at=datetime(2026, 10, 3, 9, tzinfo=UTC), full_log="第二条"),
        WorkLog(log_id="L3", source_record_id="rec3", person_id="B",
                submitted_at=datetime(2026, 10, 3, 10, tzinfo=UTC), full_log="骨干日志"),
    ]
    return SnapshotBuilder().build(workflow_run_id="run", target_date=DAY,
        organization=organization, logs=logs, now=datetime.now(UTC))


class FakeDocs:
    def __init__(self):
        self.children = {}
        self.blocks = {}
        self.counter = 0

    def find_child(self, parent, name, kind):
        return self.children.get((parent, name, kind))

    def create_folder(self, parent, name):
        self.counter += 1
        token = f"folder{self.counter}"
        self.children[(parent, name, "folder")] = token
        return token

    def create_document(self, parent, title):
        self.counter += 1
        token = f"doc{self.counter}"
        self.children[(parent, title, "docx")] = token
        return token

    def list_blocks(self, token):
        return self.blocks.get(token, [])

    def append_blocks(self, token, blocks):
        self.blocks.setdefault(token, []).extend(blocks)


class Workflow1PipelineTests(unittest.TestCase):
    def test_ai_rows_use_only_existing_columns_and_reconcile_partial_success(self):
        snapshot = sample_snapshot()
        for state in snapshot.log_evaluations.values():
            state.status = UnitStatus.WAITING_CONFIRMATION
            state.positive_ai = "肯定"
            state.improvement_ai = "改进"
        fields = {
            "evaluation_id": "业务编号", "business_key": "业务编号",
            "person_ref": "被评价日志提交人",
            "evaluator_ref": "评价人", "source_log": "工作日志",
            "evaluated_at": "评价时间", "positive_ai": "肯定之处_AI",
            "improvement_ai": "改进之处_AI",
        }
        config = TableConfig.model_validate({"tables": {
            "evaluations": {"table_id": "ai", "fields": fields}}})
        class Bitable:
            def __init__(self):
                self.records = []
                self.created = []
            def list_records(self, table_id):
                return self.records
            def batch_create(self, table_id, rows):
                self.created.extend(rows)
                result = []
                for row in rows:
                    record = {"record_id": f"rec{len(self.records) + 1}",
                        "fields": dict(row)}
                    self.records.append(record)
                    result.append(record)
                return result
        bitable = Bitable()
        with tempfile.TemporaryDirectory() as directory:
            repo = AiEvaluationRepository(bitable, config, FileStateStore(directory))
            first = repo.publish(snapshot)
            second = repo.publish(snapshot)
        self.assertEqual(first, second)
        self.assertEqual(first[1], {})
        self.assertEqual(len(bitable.created), 3)
        self.assertEqual(set(bitable.created[0]), set(fields.values()) - {"评价人"})
        self.assertTrue(bitable.created[0]["业务编号"].startswith("2026-10-03:EVAL:"))
        self.assertEqual(bitable.created[0]["工作日志"], "第一条")

    def test_log_content_uses_four_sections_without_blank_lines(self):
        log = WorkLog(
            log_id="L", person_id="M", submitted_at=datetime.now(UTC),
            progress="完成资料核对", difficulties="两条记录缺少编号",
            reflection="应先校验数据再归档", other="明天补齐编号",
            full_log="旧格式",
        )
        self.assertEqual(log.content(),
            "工作进展:完成资料核对\n"
            "工作困难:两条记录缺少编号\n"
            "心得反思:应先校验数据再归档\n"
            "其他:明天补齐编号")

    def test_report_index_uses_existing_columns_and_document_url(self):
        snapshot = sample_snapshot()
        config = TableConfig.model_validate({"tables": {"reports": {
            "table_id": "reports", "fields": {
                "title": "文本", "reporter_ref": "报告人", "role": "角色",
                "reported_at": "报告时间", "document_url": "飞书文档链接"}}}})
        class Bitable:
            def __init__(self):
                self.records = []
                self.calls = 0
            def list_records(self, table_id):
                return self.records
            def list_fields(self, table_id):
                return [{"field_name": "飞书文档链接", "type": 15}]
            def batch_create(self, table_id, rows):
                self.calls += 1
                created = [{"record_id": f"r{len(self.records) + i}",
                            "fields": row} for i, row in enumerate(rows)]
                self.records.extend(created)
                return created
        bitable = Bitable()
        with tempfile.TemporaryDirectory() as directory:
            store = FileStateStore(directory, snapshot_model=DailySnapshot)
            store.get_or_create_workflow(DAY)
            repo = WorkflowReportRepository(bitable, config, store)
            item = CloudObject(business_key=f"{DAY}:TEAM", token="doc1",
                               url="https://feishu.cn/docx/doc1")
            repo.publish(snapshot, {item.business_key: item})
            repo.publish(snapshot, {item.business_key: item})
        self.assertEqual(bitable.calls, 1)
        self.assertEqual(set(bitable.records[0]["fields"]),
            {"报告人", "角色", "报告时间", "飞书文档链接"})
        self.assertEqual(bitable.records[0]["fields"]["飞书文档链接"],
                         {"text": "10-03 团队日志", "link": item.url})

    def test_report_link_falls_back_to_existing_text_column(self):
        snapshot = sample_snapshot()
        config = TableConfig.model_validate({"tables": {"reports": {
            "table_id": "reports", "fields": {
                "title": "文本", "reporter_ref": "报告人", "role": "角色",
                "reported_at": "报告时间"}}}})
        class Bitable:
            def __init__(self):
                self.records = []
                self.calls = 0
            def list_records(self, table_id):
                return self.records
            def batch_create(self, table_id, rows):
                self.calls += 1
                created = [{"record_id": f"r{len(self.records) + i}",
                            "fields": row} for i, row in enumerate(rows)]
                self.records.extend(created)
                return created
        bitable = Bitable()
        with tempfile.TemporaryDirectory() as directory:
            store = FileStateStore(directory, snapshot_model=DailySnapshot)
            store.get_or_create_workflow(DAY)
            repo = WorkflowReportRepository(bitable, config, store)
            item = CloudObject(business_key=f"{DAY}:TEAM", token="doc1",
                               url="https://feishu.cn/docx/doc1")
            repo.publish(snapshot, {item.business_key: item})
            repo.publish(snapshot, {item.business_key: item})
        self.assertEqual(bitable.calls, 1)
        self.assertIn(item.url, bitable.records[0]["fields"]["文本"])

    def test_two_logs_keep_distinct_evaluation_states(self):
        snapshot = sample_snapshot()
        self.assertEqual(snapshot.submission_status["M"].log_ids, ["L1", "L2"])
        self.assertEqual(snapshot.evaluators["B"].log_ids, ["L1", "L2"])
        self.assertEqual(snapshot.evaluators["D"].log_ids, ["L3"])
        self.assertEqual(len(snapshot.log_evaluations), 3)

    def test_form_result_must_match_frozen_scope(self):
        snapshot = sample_snapshot()
        snapshot.log_evaluations["L1"].evaluation_id = "PJ-1"
        fields = {
            "number": "PJ-1", "person": "成员", "evaluator": "骨干",
            "positive": "具体肯定", "improvement": "具体建议",
            "author": [{"id": "ou_b"}],
        }
        config = TableConfig.model_validate({"tables": {
            "human_evaluations": {"table_id": "human", "fields": {
                "evaluation_id": "number", "person_ref": "person",
                "evaluator_ref": "evaluator", "positive_final": "positive",
                "improvement_final": "improvement", "submitted_by": "author"}}}})
        repo = HumanEvaluationRepository(Mock(), config)
        key, evaluation = repo.parse(snapshot, {"fields": fields})
        self.assertEqual(key, evaluation_key(DAY, "B", "L1"))
        self.assertEqual(evaluation.source, "HUMAN")
        with self.assertRaisesRegex(ValueError, "evaluator_ref"):
            repo.parse(snapshot, {"fields": {**fields, "evaluator": "部长"}})

    def test_form_matching_accepts_next_day_before_nineteen_only(self):
        snapshot = sample_snapshot()
        snapshot.log_evaluations["L1"].evaluation_id = "PJ-1"
        config = TableConfig.model_validate({"tables": {
            "human_evaluations": {"table_id": "human", "fields": {
                "evaluation_id": "number", "person_ref": "person",
                "evaluator_ref": "evaluator", "positive_final": "positive",
                "improvement_final": "improvement", "submitted_by": "author",
                "evaluated_at": "filled_at"}}}})
        fields = {"number": "PJ-1", "person": "成员", "evaluator": "骨干",
                  "positive": "具体肯定", "improvement": "具体建议",
                  "author": [{"id": "ou_b"}]}
        def row(hour, minute=0, **metadata):
            submitted = datetime(2026, 10, 4, hour, minute,
                                 tzinfo=ZoneInfo("Asia/Shanghai"))
            return {"fields": {**fields, "filled_at": int(submitted.timestamp() * 1000)},
                    **metadata}
        on_time = row(18, 59)
        at_cutoff = row(19)
        edited_late = row(18, 0, last_modified_time=row(19)["fields"]["filled_at"])
        repo = HumanEvaluationRepository(Mock(), config, cutoff_at="19:00")
        repo.bitable.list_records.return_value = [on_time, at_cutoff, edited_late]
        self.assertEqual(repo.for_date(snapshot), [on_time])
        self.assertIsNotNone(repo.parse(snapshot, on_time))
        self.assertIsNone(repo.parse(snapshot, at_cutoff))
        self.assertIsNone(repo.parse(snapshot, edited_late))

    def test_document_links_are_hierarchical_and_reused(self):
        snapshot = sample_snapshot()
        for state in snapshot.log_evaluations.values():
            state.status = UnitStatus.CONFIRMED
            state.positive_final = "完成"
            state.improvement_final = "继续"
        for progress in snapshot.evaluators.values():
            progress.closed = True
        with tempfile.TemporaryDirectory() as directory:
            store = FileStateStore(directory, snapshot_model=DailySnapshot)
            store.get_or_create_workflow(DAY)
            store.save_snapshot(snapshot)
            fake = FakeDocs()
            builder = DailyDocuments(fake, store, "parent")
            objects = builder.advance(DAY)
            count = fake.counter
            builder.advance(DAY)
            self.assertEqual(fake.counter, count)
            team = objects[f"{DAY}:TEAM"]
            department = objects[f"{DAY}:DEPARTMENT:dep"]
            member = objects[f"{DAY}:MEMBERS:B"]
            team_text = str(fake.blocks[team.token])
            department_text = str(fake.blocks[department.token])
            member_text = str(fake.blocks[member.token])
            self.assertIn(quote(department.url, safe=""), team_text)
            self.assertNotIn("第一条", team_text)
            self.assertIn(quote(member.url, safe=""), department_text)
            self.assertNotIn("第一条", department_text)
            self.assertIn("第一条", member_text)
            self.assertIn("第二条", member_text)
            self.assertIn("-所属部门：dep·一部", member_text)
            self.assertNotIn("rec1", member_text)
            self.assertIn("-是否为骨干：否", member_text)
            self.assertIn("-是否为骨干：是", member_text)

    def test_llm_failure_skips_and_ledger_and_resume_retries_only_failure(self):
        snapshot = sample_snapshot()
        cache = Mock()
        cache.get.return_value = snapshot.organization
        logs = Mock()
        logs.get_logs_by_date.return_value = (snapshot.logs, {})
        ai = Mock()
        ai.publish.return_value = ({"L1": ("rec_ai1", "PJ-1"),
                                    "L3": ("rec_ai3", "PJ-3")}, {})
        human = Mock()
        human.for_date.return_value = []
        docs = Mock()
        docs.advance.return_value = {}
        reports = Mock()
        reports.publish.return_value = {}
        messages = Mock()
        messages.send_text.return_value = {"message_id": "msg"}
        calls = []
        failed_once = {"L2": True}

        def execute(**kwargs):
            log_id = kwargs["input_data"].log_id
            calls.append(log_id)
            if log_id == "L2" and failed_once["L2"]:
                failed_once["L2"] = False
                raise ValueError("temporary")
            return LogPromptOutput(positive="好", improvement="继续")

        with tempfile.TemporaryDirectory() as directory:
            store = FileStateStore(directory, snapshot_model=DailySnapshot)
            prompt = Mock()
            prompt.execute.side_effect = execute
            workflow = Workflow1(
                store=store, organization_cache=cache,
                log_repository=logs, ai_evaluations=ai,
                human_evaluations=human, prompt_service=prompt,
                messages=messages, documents=docs, reports=reports,
                llm_concurrency=2, auto_advance_at="",
                prompt_path="prompts/S01.txt", admin_open_id="ou_admin")
            first = workflow.start(DAY)
            # 失败单元不再拖垮全天：当天流程照常走到等待评价状态。
            self.assertEqual(first.status, WorkflowStatus.WAITING_EVALUATIONS)
            stored = store.load_snapshot(DAY)
            self.assertIn("log:L2:ai-failed", stored.issues)
            self.assertEqual(stored.log_evaluations["L2"].status, UnitStatus.FAILED)
            self.assertTrue(stored.evaluations_published)
            workflow.recover_incomplete()  # 自动恢复路径不碰 FAILED
            self.assertEqual(calls.count("L2"), 1)
            admin_msgs = [call for call in
                          messages.send_text.call_args_list
                          if call.args and call.args[0] == "ou_admin"]
            self.assertEqual(len(admin_msgs), 1)
            # 自动重入（非 resume）绝不重评失败项。
            workflow.start(DAY)
            self.assertEqual(calls.count("L2"), 1)
            self.assertEqual(len([c for c in
                                  messages.send_text.call_args_list
                                  if c.args and c.args[0] == "ou_admin"]), 1)
            # resume（重评接口）只重置并重跑失败项。
            second = workflow.resume(DAY)
            self.assertEqual(second.status, WorkflowStatus.WAITING_EVALUATIONS)
            self.assertEqual(calls.count("L2"), 2)
            self.assertEqual(calls.count("L1"), 1)
            self.assertEqual(calls.count("L3"), 1)
            self.assertEqual(logs.get_logs_by_date.call_count, 1)


if __name__ == "__main__":
    unittest.main()

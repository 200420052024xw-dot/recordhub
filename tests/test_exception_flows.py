"""异常流程策略：跳过 + 台账 + 标注（见 docs/异常流程与重评接口.md）。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import Mock

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from data import FileStateStore
from data.repositories import LogRepository
from workflow1.snapshot import SnapshotBuilder
from workflow1.evaluations import AiEvaluationRepository
from schema import Department, Organization, Person, TableConfig, WorkLog
from workflow1.models import DailySnapshot, LogPromptOutput, UnitStatus
from tool.cloud_docs import text_block
from workflow1.documents import DailyDocuments
from workflow1.workflow import Workflow1

DAY = date(2026, 10, 3)

LOG_FIELDS = {
    "log_id": "自动编号", "submitted_at": "提交时间", "submitter_ref": "提交人",
    "progress": "工作进展：", "difficulties": "工作困难：",
    "reflection": "心得反思：", "other": "其他：", "full_log": "完整日志",
}
EVAL_FIELDS = {
    "evaluation_id": "评价编号", "person_ref": "被评价日志提交人",
    "evaluator_ref": "评价人", "source_log": "工作日志",
    "evaluated_at": "评价时间", "positive_ai": "肯定之处_AI",
    "improvement_ai": "改进之处_AI",
}


def organization(backbone_open_id: str | None = "ou_p2") -> Organization:
    return Organization(
        persons=[
            Person(person_id="P1", name="成员", role="基层学生",
                   department_id="dep", leader_id="P2", open_id="ou_p1"),
            Person(person_id="P2", name="骨干", role="骨干学生",
                   department_id="dep", leader_id="P3", open_id=backbone_open_id),
            Person(person_id="P3", name="部长", role="部长",
                   department_id="dep", open_id="ou_p3"),
        ],
        departments=[Department(department_id="dep", name="一部", minister_id="P3")],
    )


class BitableStub:
    def __init__(self, records):
        self.records = records
        self.created = []

    def list_records(self, table_id, **kwargs):
        return self.records

    def batch_create(self, table_id, rows):
        out = []
        for row in rows:
            record = {"record_id": f"c{len(self.records) + 1}", "fields": dict(row)}
            self.records.append(record)
            self.created.append(row)
            out.append(record)
        return out


def log_record(record_id: str, log_id: str, submitter: str,
                submitted_at: str | None = None) -> dict:
    fields = {"自动编号": log_id, "提交人": submitter, "完整日志": log_id}
    if submitted_at:
        fields["提交时间"] = submitted_at
    return {"record_id": record_id, "fields": fields}


class LogReadIssueTests(unittest.TestCase):
    def repository(self, records) -> LogRepository:
        config = TableConfig.model_validate({"tables": {
            "logs": {"table_id": "tbl_logs", "fields": LOG_FIELDS}}})
        return LogRepository(BitableStub(records), config, Mock())

    def test_missing_time_interpolated_from_previous_record(self):
        records = [
            log_record("r1", "L1", "ou_p1", "2026-10-03T08:00:00+08:00"),
            log_record("r2", "L2", "ou_p2"),
            log_record("r3", "L3", "ou_p3", "2026-10-04T09:00:00+08:00"),
        ]
        logs, issues = self.repository(records).get_logs_by_date(
            DAY, organization=organization())
        self.assertEqual([log.log_id for log in logs], ["L1", "L2"])
        self.assertIn("log:L2:time-interpolated", issues)

    def test_unknown_submitter_is_skipped_and_ledgered(self):
        records = [log_record("r1", "L9", "ou_ghost", "2026-10-03T08:00:00+08:00")]
        logs, issues = self.repository(records).get_logs_by_date(
            DAY, organization=organization())
        self.assertEqual(logs, [])
        self.assertIn("log:L9:unknown-submitter", issues)
        self.assertIn("不在人员表", issues["log:L9:unknown-submitter"])

    def test_same_person_same_day_keeps_latest(self):
        records = [
            log_record("r1", "L1", "ou_p1", "2026-10-03T08:00:00+08:00"),
            log_record("r2", "L2", "ou_p1", "2026-10-03T09:00:00+08:00"),
        ]
        logs, issues = self.repository(records).get_logs_by_date(
            DAY, organization=organization())
        self.assertEqual([log.log_id for log in logs], ["L2"])
        self.assertIn("log:L1:superseded", issues)


def build_snapshot(logs: list[WorkLog],
                   org: Organization | None = None):
    return SnapshotBuilder().build(
        workflow_run_id="run", target_date=DAY,
        organization=org or organization(), logs=logs, now=datetime.now(UTC))


class MissingMarkerRowTests(unittest.TestCase):
    def test_unsubmitted_person_gets_marker_row_once(self):
        logs = [WorkLog(log_id="LA", source_record_id="rec_a", person_id="P2",
                        submitted_at=datetime(2026, 10, 3, 10, tzinfo=UTC),
                        full_log="骨干日志")]
        snapshot = build_snapshot(logs)
        state = snapshot.log_evaluations["LA"]
        state.status = UnitStatus.WAITING_CONFIRMATION
        state.positive_ai = state.positive_final = "好"
        state.improvement_ai = state.improvement_final = "继续"
        config = TableConfig.model_validate({"tables": {
            "evaluations": {"table_id": "ai", "fields": EVAL_FIELDS}}})
        bitable = BitableStub([])
        repository = AiEvaluationRepository(bitable, config, FileStateStore(
            tempfile.mkdtemp()))
        _, issues = repository.publish(snapshot)
        repository.publish(snapshot)
        self.assertEqual(issues, {})
        self.assertEqual(len(bitable.created), 2)
        marker = next(row for row in bitable.created
                      if row["评价编号"] == f"{DAY}:MISSING:P1")
        self.assertEqual(marker["肯定之处_AI"], "未填写日志")
        self.assertEqual(marker["改进之处_AI"], "未填写日志")
        self.assertEqual(marker["被评价日志提交人"], [{"id": "ou_p1"}])
        self.assertNotIn("工作日志", marker)


class DocumentConflictTests(unittest.TestCase):
    class EditedDocs:
        def __init__(self, edited_titles=()):
            self.children = {}
            self.blocks = {}
            self.counter = 0
            self.edited_titles = set(edited_titles)

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
            if title in self.edited_titles:
                self.blocks[token] = [text_block("人工修改的内容")]
            return token

        def list_blocks(self, token):
            return self.blocks.get(token, [])

        def append_blocks(self, token, blocks):
            self.blocks.setdefault(token, []).extend(blocks)

    def test_manual_edit_wins_and_flow_continues(self):
        logs = [WorkLog(log_id="LA", source_record_id="rec_a", person_id="P1",
                        submitted_at=datetime(2026, 10, 3, 10, tzinfo=UTC),
                        full_log="成员日志")]
        snapshot = build_snapshot(logs)
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
            fake = self.EditedDocs(edited_titles={"10-03 骨干同学小组日志"})
            builder = DailyDocuments(fake, store, "parent")
            objects = builder.advance(DAY)
            stored = store.load_snapshot(DAY)
            self.assertIn(f"doc:{DAY}:MEMBERS:P2:manual-edit", stored.issues)
            self.assertTrue(stored.cloud_objects[
                f"{DAY}:MEMBERS:P2"].content_written)
            self.assertIn(f"{DAY}:DEPARTMENT:dep", objects)
            self.assertIn(f"{DAY}:TEAM", objects)
            builder.advance(DAY)
            self.assertEqual(stored.cloud_objects[
                f"{DAY}:MEMBERS:P2"].content_written, True)


class NotifySkipTests(unittest.TestCase):
    def test_evaluator_without_open_id_is_skipped_and_ledgered(self):
        org = organization(backbone_open_id=None)
        logs = [WorkLog(log_id="LA", source_record_id="rec_a", person_id="P1",
                        submitted_at=datetime(2026, 10, 3, 10, tzinfo=UTC),
                        full_log="成员日志")]
        snapshot = build_snapshot(logs, org)
        cache = Mock()
        cache.get.return_value = snapshot.organization
        logs_repository = Mock()
        logs_repository.get_logs_by_date.return_value = (snapshot.logs, {})
        ai = Mock()
        ai.publish.return_value = ({}, {})
        human = Mock()
        human.for_date.return_value = []
        docs = Mock()
        docs.advance.return_value = {}
        reports = Mock()
        reports.publish.return_value = {}
        messages = Mock()
        prompt = Mock()
        prompt.execute.return_value = LogPromptOutput(positive="好", improvement="继续")
        with tempfile.TemporaryDirectory() as directory:
            workflow = Workflow1(
                store=FileStateStore(directory, snapshot_model=DailySnapshot), organization_cache=cache,
                log_repository=logs_repository, ai_evaluations=ai,
                human_evaluations=human, prompt_service=prompt,
                messages=messages, documents=docs, reports=reports,
                llm_concurrency=1, auto_advance_at="",
                prompt_path="prompts/S01.txt")
            workflow.start(DAY)
            stored = workflow.store.load_snapshot(DAY)
            self.assertIn("person:P2:missing-open-id", stored.issues)
            self.assertTrue(stored.evaluators["P2"].notified)
            messages.send_text.assert_not_called()


class MessageFormatTests(unittest.TestCase):
    def test_salutation_by_role(self):
        logs = [
            WorkLog(log_id="LA", source_record_id="rec_a", person_id="P1",
                    submitted_at=datetime(2026, 10, 3, 10, tzinfo=UTC),
                    full_log="成员日志"),
            WorkLog(log_id="LB", source_record_id="rec_b", person_id="P2",
                    submitted_at=datetime(2026, 10, 3, 11, tzinfo=UTC),
                    full_log="骨干日志"),
        ]
        snapshot = build_snapshot(logs)
        cache = Mock()
        cache.get.return_value = snapshot.organization
        logs_repository = Mock()
        logs_repository.get_logs_by_date.return_value = (snapshot.logs, {})
        ai = Mock()
        ai.publish.return_value = ({}, {})
        human = Mock()
        human.for_date.return_value = []
        docs = Mock()
        docs.advance.return_value = {}
        reports = Mock()
        reports.publish.return_value = {}
        messages = Mock()
        messages.send_text.return_value = {"message_id": "msg"}
        prompt = Mock()
        prompt.execute.return_value = LogPromptOutput(positive="好", improvement="继续")
        with tempfile.TemporaryDirectory() as directory:
            workflow = Workflow1(
                store=FileStateStore(directory, snapshot_model=DailySnapshot), organization_cache=cache,
                log_repository=logs_repository, ai_evaluations=ai,
                human_evaluations=human, prompt_service=prompt,
                messages=messages, documents=docs, reports=reports,
                llm_concurrency=1, auto_advance_at="",
                prompt_path="prompts/S01.txt")
            workflow.start(DAY)
        messages = {call.args[0]: call.args[1]
                    for call in messages.send_text.call_args_list}
        self.assertTrue(messages["ou_p2"].startswith("骨干同学，请评价以下成员 10月3日 的工作日志"))
        self.assertIn("成员", messages["ou_p2"])
        self.assertTrue(messages["ou_p3"].startswith("部长老师，请评价以下骨干 10月3日 的工作日志"))


if __name__ == "__main__":
    unittest.main()

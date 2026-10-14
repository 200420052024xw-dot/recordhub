from __future__ import annotations

import tempfile
import sys
from urllib.parse import quote
import unittest
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
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
                                        HumanEvaluationRepository, evaluation_key,
                                        review_day_timestamp)
from workflow1.workflow import (FAILED_EVALUATION_TEXT, RETRY_DELAY, RETRY_LIMIT,
                                UNPARSED_IMPROVEMENT, Workflow1)
from workflow1.reports import WorkflowReportRepository
from workflow1.models import LogPromptOutput, WorkflowStatus
from llm.errors import LLMError, LLMValidationError
from service.alerts import AdminAlertHandler
from tool.errors import StructuredOutputError

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
    def test_log_prompt_is_selected_by_frozen_evaluator(self):
        snapshot = sample_snapshot()
        prompt_repository = Mock()
        prompt_repository.resolve.side_effect = lambda code, user_id: Mock(
            template={"B": "骨干个人Prompt", "D": "部长个人Prompt"}[user_id],
            record_id=f"rec_{user_id}")
        prompt_service = Mock()
        prompt_service.execute.side_effect = lambda **kwargs: LogPromptOutput(
            positive=kwargs["template"], improvement="继续")
        with tempfile.TemporaryDirectory() as directory:
            store = FileStateStore(directory, snapshot_model=DailySnapshot)
            run = store.get_or_create_workflow(DAY)
            snapshot.workflow_run_id = run.workflow_run_id
            store.save_snapshot(snapshot)
            workflow = Workflow1(
                store=store, organization_cache=Mock(), log_repository=Mock(),
                ai_evaluations=Mock(), human_evaluations=Mock(),
                prompt_service=prompt_service, prompt_repository=prompt_repository,
                messages=Mock(), documents=Mock(), reports=Mock(), llm_concurrency=1)

            workflow._process_logs(DAY)

            saved = store.load_snapshot(DAY)
            self.assertEqual(saved.log_evaluations["L1"].positive_ai, "骨干个人Prompt")
            self.assertEqual(saved.log_evaluations["L3"].positive_ai, "部长个人Prompt")
            self.assertEqual(saved.log_evaluations["L1"].prompt_record_id, "rec_B")
            self.assertEqual(saved.log_evaluations["L3"].prompt_record_id, "rec_D")

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
        expected = int(datetime(2026, 10, 4, 0, 0,
                                tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
        self.assertEqual(review_day_timestamp(DAY), expected)
        self.assertTrue(all(row["评价时间"] == expected for row in bitable.created))

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
        self.assertEqual(repo.for_date(snapshot), [{**on_time, "_table_id": "human"}])
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
        alerts = AdminAlertHandler(messages, "ou_admin")
        logging.getLogger().addHandler(alerts)
        self.addCleanup(alerts.close)
        self.addCleanup(logging.getLogger().removeHandler, alerts)
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
            alerts.queue.join()
            # 失败单元不再拖垮全天：当天流程照常走到等待评价状态。
            self.assertEqual(first.status, WorkflowStatus.WAITING_EVALUATIONS)
            stored = store.load_snapshot(DAY)
            # 单次失败只延后重试：不记台账、不惊动管理员，等 30 分钟后自动重跑。
            self.assertNotIn("log:L2:ai-failed", stored.issues)
            self.assertEqual(stored.log_evaluations["L2"].status, UnitStatus.FAILED)
            self.assertEqual(stored.log_evaluations["L2"].retry_count, 1)
            self.assertIsNotNone(stored.log_evaluations["L2"].retry_after)
            self.assertTrue(stored.evaluations_published)
            expected_review_time = datetime(2026, 10, 4, 0, 0,
                                            tzinfo=ZoneInfo("Asia/Shanghai"))
            self.assertEqual(stored.log_evaluations["L1"].ai_evaluated_at,
                             expected_review_time)
            self.assertEqual(stored.log_evaluations["L3"].evaluated_at,
                             expected_review_time)
            workflow.recover_incomplete()  # 自动恢复路径不碰 FAILED
            self.assertEqual(calls.count("L2"), 1)
            admin_msgs = [call for call in
                          messages.send_text.call_args_list
                          if call.args and call.args[0] == "ou_admin"]
            self.assertEqual(len(admin_msgs), 0)  # 还没到"重试也不管用"的地步
            # 自动重入（非 resume）绝不重评失败项。
            workflow.start(DAY)
            alerts.queue.join()
            self.assertEqual(calls.count("L2"), 1)
            self.assertEqual(len([c for c in
                                  messages.send_text.call_args_list
                                  if c.args and c.args[0] == "ou_admin"]), 0)
            # resume（重评接口）只重置并重跑失败项。
            second = workflow.resume(DAY)
            self.assertEqual(second.status, WorkflowStatus.WAITING_EVALUATIONS)
            self.assertEqual(calls.count("L2"), 2)
            self.assertEqual(calls.count("L1"), 1)
            self.assertEqual(calls.count("L3"), 1)
            self.assertEqual(logs.get_logs_by_date.call_count, 1)

    def test_unparseable_ai_output_is_kept_in_the_positive_field(self):
        """解析不了不等于丢掉：原始输出原样进「肯定之处」，改进栏指向它，照常等人工确认。"""
        snapshot = sample_snapshot()
        cache = Mock()
        cache.get.return_value = snapshot.organization
        logs = Mock()
        logs.get_logs_by_date.return_value = (snapshot.logs, {})
        ai = Mock()
        ai.publish.return_value = ({}, {})
        human = Mock()
        human.for_date.return_value = []
        messages = Mock()
        messages.send_text.return_value = {"message_id": "msg"}
        raw = "只有一段话，没有分成两个字段：本周完成了检查工具。"

        def execute(**kwargs):
            if kwargs["input_data"].log_id == "L2":
                raise LLMValidationError("Structured output validation failed") from \
                    StructuredOutputError("Structured output validation failed", raw=raw)
            return LogPromptOutput(positive="好", improvement="继续")

        with tempfile.TemporaryDirectory() as directory:
            store = FileStateStore(directory, snapshot_model=DailySnapshot)
            prompt = Mock()
            prompt.execute.side_effect = execute
            workflow = Workflow1(
                store=store, organization_cache=cache,
                log_repository=logs, ai_evaluations=ai,
                human_evaluations=human, prompt_service=prompt,
                messages=messages, documents=Mock(advance=Mock(return_value={})),
                reports=Mock(publish=Mock(return_value={})),
                llm_concurrency=2, auto_advance_at="",
                prompt_path="prompts/S01.txt", admin_open_id="ou_admin")
            workflow.start(DAY)

            stored = store.load_snapshot(DAY)
            state = stored.log_evaluations["L2"]
            self.assertEqual(state.status, UnitStatus.WAITING_CONFIRMATION)
            self.assertEqual(state.positive_ai, raw)
            self.assertEqual(state.positive_final, raw)
            self.assertEqual(state.improvement_ai, UNPARSED_IMPROVEMENT)
            self.assertEqual(state.improvement_final, UNPARSED_IMPROVEMENT)
            self.assertIn("log:L2:ai-unparsed", stored.issues)
            # 降级项仍然会发布到评价表，人工可以照常核对确认。
            published = ai.publish.call_args.args[0]
            self.assertIn("L2", published.log_evaluations)

    def _api_failure_workflow(self, directory, *, fail_times: int,
                              retry_minutes: int = 60):
        """L2 的评价前 fail_times 次报接口错误，之后成功。"""
        snapshot = sample_snapshot()
        cache = Mock()
        cache.get.return_value = snapshot.organization
        logs = Mock()
        logs.get_logs_by_date.return_value = (snapshot.logs, {})
        messages = Mock()
        messages.send_text.return_value = {"message_id": "msg"}
        alerts = AdminAlertHandler(messages, "ou_admin")
        logging.getLogger().addHandler(alerts)
        self.addCleanup(alerts.close)
        self.addCleanup(logging.getLogger().removeHandler, alerts)
        remaining = {"L2": fail_times}

        def execute(**kwargs):
            if kwargs["input_data"].log_id == "L2" and remaining["L2"] > 0:
                remaining["L2"] -= 1
                raise LLMError("DeepSeek API request failed")
            return LogPromptOutput(positive="好", improvement="继续")

        store = FileStateStore(directory, snapshot_model=DailySnapshot)
        prompt = Mock()
        prompt.execute.side_effect = execute
        workflow = Workflow1(
            store=store, organization_cache=cache, log_repository=logs,
            ai_evaluations=Mock(publish=Mock(return_value=({}, {}))),
            human_evaluations=Mock(for_date=Mock(return_value=[])),
            prompt_service=prompt, messages=messages,
            documents=Mock(advance=Mock(return_value={})),
            reports=Mock(publish=Mock(return_value={})),
            llm_concurrency=1, auto_advance_at="",
            llm_retry_interval_minutes=retry_minutes,
            prompt_path="prompts/S01.txt", admin_open_id="ou_admin")
        return workflow, store, messages, alerts

    def test_retry_interval_is_configurable(self):
        """重试间隔来自配置：默认一小时，设成别的就按别的来。"""
        with tempfile.TemporaryDirectory() as directory:
            workflow, store, _, _ = self._api_failure_workflow(
                directory, fail_times=1, retry_minutes=240)
            before = datetime.now(UTC)

            workflow.start(DAY)

            retry_after = store.load_snapshot(DAY).log_evaluations["L2"].retry_after
            self.assertGreaterEqual(retry_after - before, timedelta(minutes=239))
            self.assertLessEqual(retry_after - before, timedelta(minutes=241))

    def test_failed_evaluation_is_retried_after_the_delay(self):
        """接口失败先延后，不到点不重跑，到点自动重跑，成功就当没事发生。"""
        with tempfile.TemporaryDirectory() as directory:
            workflow, store, messages, _ = self._api_failure_workflow(
                directory, fail_times=1)
            workflow.start(DAY)
            self.assertEqual(store.load_snapshot(DAY).log_evaluations["L2"].status,
                             UnitStatus.FAILED)

            workflow.retry_failed_evaluations()  # 还没到点
            self.assertEqual(store.load_snapshot(DAY).log_evaluations["L2"].status,
                             UnitStatus.FAILED)

            later = datetime.now(UTC) + RETRY_DELAY + timedelta(minutes=1)
            with patch("workflow1.workflow.utc_now", return_value=later):
                retried = workflow.retry_failed_evaluations()

            self.assertEqual(retried, [DAY.isoformat()])
            state = store.load_snapshot(DAY).log_evaluations["L2"]
            self.assertEqual(state.status, UnitStatus.WAITING_CONFIRMATION)
            self.assertEqual(state.positive_final, "好")
            self.assertEqual(state.fallback, "")
            self.assertEqual(state.retry_after, None)
            self.assertEqual([call for call in messages.send_text.call_args_list
                              if call.args and call.args[0] == "ou_admin"], [])

    def test_exhausted_retries_mark_for_manual_review_and_alert_admin(self):
        """三次重试都不管用：标成待人工补评、写进评价表、只报管理员不打扰老师。"""
        with tempfile.TemporaryDirectory() as directory:
            workflow, store, messages, alerts = self._api_failure_workflow(
                directory, fail_times=RETRY_LIMIT + 1)
            clock = {"now": datetime.now(UTC)}

            def tick():
                clock["now"] += RETRY_DELAY + timedelta(minutes=1)
                return clock["now"]

            with patch("workflow1.workflow.utc_now", side_effect=tick):
                workflow.start(DAY)                     # 首次失败 → 延后
                for _ in range(RETRY_LIMIT):            # 三次重试全部失败
                    workflow.retry_failed_evaluations()
                    alerts.queue.join()
            stored = store.load_snapshot(DAY)
            state = stored.log_evaluations["L2"]
            self.assertEqual(state.status, UnitStatus.WAITING_CONFIRMATION)
            self.assertEqual(state.positive_final, FAILED_EVALUATION_TEXT)
            self.assertEqual(state.improvement_final, FAILED_EVALUATION_TEXT)
            self.assertEqual(state.fallback, "failed")
            self.assertIn("log:L2:ai-failed", stored.issues)
            admin_msgs = [call for call in messages.send_text.call_args_list
                          if call.args and call.args[0] == "ou_admin"]
            self.assertEqual(len(admin_msgs), 1)
            # 老师只收到例行的"请评价日志"，不该收到任何失败相关的通知。
            teacher_msgs = [call.args[1] for call in messages.send_text.call_args_list
                            if call.args and call.args[0] in {"ou_b", "ou_m"}]
            self.assertTrue(all("失败" not in text for text in teacher_msgs),
                            teacher_msgs)

    def test_final_retry_does_not_alert_for_normal_manual_confirmation_wait(self):
        """人工审核尚未截止，AI 已完成且已发布时不应在凌晨报警。"""
        with tempfile.TemporaryDirectory() as directory:
            workflow, store, messages, alerts = self._api_failure_workflow(
                directory, fail_times=0)
            clock = datetime(2026, 10, 3, 20, 20, tzinfo=UTC)  # 北京次日 04:20
            with patch("workflow1.workflow.utc_now", return_value=clock):
                workflow.start(DAY)
                calls = workflow.prompt_service.execute.call_count
                retried = workflow.retry_incomplete(final=True)
                self.assertEqual(retried[0]["status"], WorkflowStatus.WAITING_EVALUATIONS)
                stored = store.load_snapshot(DAY)
                self.assertTrue(stored.evaluations_published)
                self.assertNotIn("workflow1:manual-review", stored.issues)
                self.assertEqual(workflow.prompt_service.execute.call_count, calls)
                # 后续自动补跑仍可执行，没有被误写的人工排查台账阻止。
                self.assertEqual(len(workflow.retry_incomplete(final=True)), 1)
            alerts.queue.join()
            self.assertEqual([call for call in messages.send_text.call_args_list
                              if call.args and call.args[0] == "ou_admin"], [])

    def test_final_retry_still_alerts_for_abnormal_waiting_states(self):
        for case in ("unpublished", "failed", "fallback", "error",
                     "confirmed_without_report", "overdue"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                workflow, store, _, alerts = self._api_failure_workflow(directory, fail_times=0)
                clock = datetime(2026, 10, 3, 20, 20, tzinfo=UTC)
                with patch("workflow1.workflow.utc_now", return_value=clock):
                    workflow.start(DAY)
                    def mutate(snapshot):
                        if case == "unpublished":
                            snapshot.evaluations_published = False
                        elif case == "failed":
                            snapshot.log_evaluations["L2"].status = UnitStatus.FAILED
                        elif case == "fallback":
                            snapshot.log_evaluations["L2"].fallback = "failed"
                        elif case == "error":
                            snapshot.log_evaluations["L2"].error = "评价处理失败"
                        elif case == "confirmed_without_report":
                            for state in snapshot.log_evaluations.values():
                                state.status = UnitStatus.CONFIRMED
                    store.update_snapshot(DAY, mutate)
                    # 保留异常状态以检查最后一轮的告警判据。
                    with patch.object(workflow, "start", return_value=store.load_workflow(DAY)), \
                            patch.object(workflow, "_deadline_passed", return_value=case == "overdue"):
                        workflow.retry_incomplete(final=True)
                    self.assertIn("workflow1:manual-review", store.load_snapshot(DAY).issues)
                alerts.queue.join()

    def test_incomplete_day_is_retried_then_flagged_for_manual_review(self):
        """自动处理失败的当天逐小时重试；最后一轮仍异常则标记人工排查。"""
        with tempfile.TemporaryDirectory() as directory:
            workflow, store, messages, alerts = self._api_failure_workflow(
                directory, fail_times=RETRY_LIMIT + 1)
            clock = datetime(2026, 10, 4, 2, 20, tzinfo=UTC)   # 北京 10-04 10:20
            with patch("workflow1.workflow.utc_now", return_value=clock):
                workflow.start(DAY)
                self.assertNotIn("workflow1:manual-review",
                                 store.load_snapshot(DAY).issues)

                retried = workflow.retry_incomplete(final=True)

                self.assertEqual([item["target_date"] for item in retried],
                                 [DAY.isoformat()])
                self.assertNotEqual(retried[0]["status"], WorkflowStatus.COMPLETED)
                self.assertIn("workflow1:manual-review",
                              store.load_snapshot(DAY).issues)
                alerts.queue.join()
                admin_msgs = [call for call in messages.send_text.call_args_list
                              if call.args and call.args[0] == "ou_admin"]
                self.assertEqual(len(admin_msgs), 1)

                # 已经标过人工补评：不再自动重试，也不再重复报警。
                self.assertEqual(workflow.retry_incomplete(final=True), [])
                alerts.queue.join()
                self.assertEqual(len([call for call in messages.send_text.call_args_list
                                      if call.args and call.args[0] == "ou_admin"]), 1)

    def test_unparsed_marker_stays_out_of_the_report(self):
        """「详情见肯定之处」是给填表人的指路话，报告里只写内容本身。"""
        snapshot = sample_snapshot()
        state = snapshot.log_evaluations["L2"]
        state.positive_final = "本周完成了检查工具。"
        state.improvement_final = UNPARSED_IMPROVEMENT
        state.fallback = "unparsed"
        documents = DailyDocuments.__new__(DailyDocuments)

        text = str(documents._evaluation_blocks(snapshot, state))

        self.assertIn("本周完成了检查工具", text)
        self.assertNotIn("详情见", text)
        self.assertNotIn("-改进之处", text)


if __name__ == "__main__":
    unittest.main()

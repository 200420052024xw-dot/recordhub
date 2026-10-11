from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import Mock

from data import FileStateStore
from llm import LLMError, PromptRepository, PromptService
from llm.errors import LLMValidationError
from schema import Organization, Person, TableConfig
from tool.errors import FeishuApiError


class FakeBitable:
    def __init__(self, records, fields=None, list_fields_errors=0):
        self.records = deepcopy(records)
        # table_id -> 字段元数据;缺省时任何表都返回空列表。
        self.fields = deepcopy(fields or {})
        # 前 N 次读字段定义抛错，用来模拟接口抖动。
        self.list_fields_errors = list_fields_errors
        self.list_fields_calls = 0
        self.updates = []

    def list_fields(self, table_id):
        self.list_fields_calls += 1
        if self.list_fields_errors > 0:
            self.list_fields_errors -= 1
            raise FeishuApiError("fields unavailable")
        return deepcopy(self.fields.get(table_id, []))

    def list_records(self, table_id):
        return deepcopy(self.records)

    def update_record(self, table_id, record_id, fields):
        self.updates.append((record_id, dict(fields)))
        row = next(item for item in self.records if item["record_id"] == record_id)
        row["fields"].update(fields)
        return deepcopy(row)


def table_config() -> TableConfig:
    return TableConfig.model_validate({"tables": {"prompts": {
        "table_id": "tblPrompts",
        "fields": {
            "user_ref": "使用人", "role": "角色", "template": "Skill内容",
            "function": "功能", "review_result": "审核结果",
            "failure_reason": "未通过原因",
        },
    }}})


def organization() -> Organization:
    return Organization(persons=[Person(
        person_id="M1", name="部长甲", role="部长", open_id="ou_m1",
        source_record_id="rec_person",
    )], departments=[])


def row(record_id: str, status: str, template: str, created_time: int):
    return {
        "record_id": record_id,
        "created_time": created_time,
        "fields": {
            "使用人": [{"record_id": "rec_person"}],
            "角色": "部长",
            "Skill内容": template,
            "功能": "日志评价",
            "审核结果": status,
            "未通过原因": "",
        },
    }


class PromptRepositoryTests(unittest.TestCase):
    def make_repository(self, records, service=None, fields=None,
                        list_fields_errors=0):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        bitable = FakeBitable(records, fields, list_fields_errors)
        messages = Mock()
        messages.send_text.return_value = {"message_id": "msg1"}
        repository = PromptRepository(
            bitable=bitable, tables=table_config(),
            store=FileStateStore(temporary.name),
            prompt_service=service or Mock(), messages=messages,
        )
        return repository, bitable, messages

    def test_blank_new_version_passes_and_supersedes_previous(self):
        service = Mock()
        repository, bitable, messages = self.make_repository([
            row("rec_old", "通过", "旧版", 1),
            row("rec_new", "", "新版", 2),
        ], service)

        result = repository.review_pending(organization())

        self.assertEqual(result, {"reviewed": 1, "passed": 1, "failed": 0})
        self.assertEqual(repository.resolve("S01", "M1").record_id, "rec_new")
        self.assertEqual(bitable.records[0]["fields"]["审核结果"], "未使用")
        self.assertEqual(bitable.records[1]["fields"]["审核结果"], "通过")
        service.execute.assert_called_once()
        messages.send_text.assert_not_called()
        restarted = PromptRepository(
            bitable=bitable, tables=table_config(), store=repository.store,
            prompt_service=Mock(), messages=messages)
        self.assertEqual(restarted.resolve("S01", "M1").record_id, "rec_new")

    def test_failed_new_version_keeps_previous_and_notifies_user(self):
        service = Mock()
        service.execute.side_effect = LLMValidationError("missing positive")
        repository, bitable, messages = self.make_repository([
            row("rec_old", "通过", "旧版", 1),
            row("rec_bad", "", "坏版", 2),
        ], service)

        result = repository.review_pending(organization())

        self.assertEqual(result, {"reviewed": 1, "passed": 0, "failed": 1})
        self.assertEqual(repository.resolve("S01", "M1").record_id, "rec_old")
        self.assertEqual(bitable.records[0]["fields"]["审核结果"], "通过")
        self.assertEqual(bitable.records[1]["fields"]["审核结果"], "未通过")
        self.assertIn("missing positive", bitable.records[1]["fields"]["未通过原因"])
        messages.send_text.assert_called_once()
        self.assertEqual(messages.send_text.call_args.args[0], "ou_m1")

    def test_interface_failure_defers_without_blaming_the_user(self):
        """接口故障不是模板的错：不写“未通过”、不通知本人，留空等下轮重审。"""
        service = Mock()
        service.execute.side_effect = LLMError("DeepSeek API request failed")
        repository, bitable, messages = self.make_repository([
            row("rec_old", "通过", "旧版", 1),
            row("rec_new", "", "新版", 2),
        ], service)

        result = repository.review_pending(organization())

        self.assertEqual(result, {"reviewed": 0, "passed": 0, "failed": 0})
        self.assertEqual(repository.resolve("S01", "M1").record_id, "rec_old")
        self.assertEqual(bitable.records[1]["fields"]["审核结果"], "")
        messages.send_text.assert_not_called()
        # 留空才会被下一轮审核重新捡起来。
        self.assertEqual(bitable.records[1]["fields"]["未通过原因"], "")

    def test_only_blank_rows_are_audited(self):
        service = Mock()
        repository, _, _ = self.make_repository([
            row("rec_active", "通过", "当前", 3),
            row("rec_old", "未使用", "历史", 2),
            row("rec_bad", "未通过", "失败", 1),
        ], service)
        result = repository.review_pending(organization())
        self.assertEqual(result["reviewed"], 0)
        service.execute.assert_not_called()

    def test_fixed_audit_rejects_missing_output_fields(self):
        client = Mock()
        client.complete_json.side_effect = lambda messages, validator: validator(
            {"positive": "只有一个字段"})
        service = PromptService(client, max_attempts=1)
        repository, _, _ = self.make_repository([], service)
        from llm.prompt_repository import PromptEntry
        entry = PromptEntry(
            user_id="M1", skill_code="S01", function="日志评价",
            template="测试", record_id="rec", reviewed_at=datetime.now(timezone.utc),
        )
        with self.assertRaises(LLMError):
            repository._audit(entry)

    def table_fields(self, status_options=None) -> dict:
        """按线上真实结构造字段元数据：角色是查找引用，功能/审核结果是单选。"""
        prompts = [
            {"field_id": "fldUser", "field_name": "使用人", "type": 11},
            {"field_id": "fldRole", "field_name": "角色", "type": 19, "property": {
                "target_field": "fldSourceRole",
                "filter_info": {"target_table": "tblPersons"}}},
            {"field_id": "fldFunction", "field_name": "功能", "type": 3, "property": {
                "options": [{"id": "optpgkLG68", "name": "日志评价"}]}},
            {"field_id": "fldStatus", "field_name": "审核结果", "type": 3, "property": {
                "options": status_options or [
                    {"id": "optvJCrE7O", "name": "通过"},
                    {"id": "opt3UJX6wP", "name": "未通过"}]}},
        ]
        return {"tblPrompts": prompts, "tblPersons": [
            {"field_id": "fldSourceRole", "field_name": "角色", "type": 3,
             "property": {"options": [{"id": "optwbBg3XN", "name": "部长"},
                                      {"id": "optt2cmYIv", "name": "骨干学生"}]}},
        ]}

    def test_function_option_id_maps_to_the_skill_code(self):
        """功能列回选项 ID 时也要能对上 FUNCTION_CODES，否则整条被判“未知功能”。"""
        item = row("rec_new", "", "新版", 1)
        item["fields"]["功能"] = ["optpgkLG68"]
        repository, _, messages = self.make_repository(
            [item], fields=self.table_fields())

        result = repository.review_pending(organization())

        self.assertEqual(result, {"reviewed": 1, "passed": 1, "failed": 0})
        self.assertEqual(repository.resolve("S01", "M1").record_id, "rec_new")
        messages.send_text.assert_not_called()

    def test_lookup_option_id_is_resolved_from_the_source_field(self):
        """查找引用只回传选项 ID，它的选项表要回到源表（人员表）的源字段去取。"""
        repository, _, _ = self.make_repository([], fields=self.table_fields())

        self.assertEqual(repository._field_choice(["optwbBg3XN"]), "部长")

    def test_failed_field_metadata_load_is_not_cached(self):
        """读字段定义失败不能当成“表里没选项”缓存：整个进程只有这一次机会。"""
        repository, _, _ = self.make_repository(
            [], fields=self.table_fields(), list_fields_errors=1)

        # 第一次没读到，编号还原不出来，但失败不能被记住。
        self.assertEqual(repository._field_choice("optvJCrE7O"), "optvJCrE7O")
        # 第二次重试成功，编号照常还原。
        self.assertEqual(repository._field_choice("optvJCrE7O"), "通过")

    def test_successful_field_metadata_load_is_cached(self):
        repository, bitable, _ = self.make_repository([], fields=self.table_fields())

        repository._field_choice("optvJCrE7O")
        loaded = bitable.list_fields_calls
        repository._field_choice("optvJCrE7O")

        self.assertEqual(bitable.list_fields_calls, loaded)

    def test_review_result_option_id_is_recognized(self):
        fields = self.table_fields(status_options=[{"id": "optPass0001", "name": "通过"}])
        repository, _, _ = self.make_repository(
            [dict(row("rec_active", ["optPass0001"], "当前", 3))], fields=fields)

        result = repository.review_pending(organization())

        self.assertEqual(result["reviewed"], 0)
        self.assertEqual(repository.resolve("S01", "M1").record_id, "rec_active")


if __name__ == "__main__":
    unittest.main()

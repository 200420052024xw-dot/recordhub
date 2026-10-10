from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import Mock

from data import FileStateStore
from llm import LLMError, PromptRepository, PromptService
from schema import Organization, Person, TableConfig


class FakeBitable:
    def __init__(self, records):
        self.records = deepcopy(records)
        self.updates = []

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
    def make_repository(self, records, service=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        bitable = FakeBitable(records)
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
        service.execute.side_effect = LLMError("missing positive")
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


if __name__ == "__main__":
    unittest.main()

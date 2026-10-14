from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from collections import deque
from pathlib import Path
from typing import Any

from llm.client import DeepSeekClient
from config import DeepSeekSettings, FeishuSettings
from tool.bitable_fields import scalar
from tool.feishu import BitableService, ContactService, FeishuClient, FeishuTokenProvider, MessageService
from tool.http import HttpResponse
from tool.errors import FeishuApiError
from tool.cloud_docs import CloudDocsService
from config import load_schedule_config


class FakeTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = deque(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> HttpResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.popleft()


def response(data: dict[str, Any], status: int = 200) -> HttpResponse:
    return HttpResponse(status_code=status, data=data, headers={})


class FoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.feishu_settings = FeishuSettings(
            app_id="app", app_secret="secret", bitable_app_token="base"
        )

    def test_token_is_cached(self) -> None:
        transport = FakeTransport(
            [response({"code": 0, "tenant_access_token": "token", "expire": 7200})]
        )
        provider = FeishuTokenProvider(self.feishu_settings, transport)
        self.assertEqual(provider.get_token(), "token")
        self.assertEqual(provider.get_token(), "token")
        self.assertEqual(len(transport.calls), 1)

    def test_bitable_pagination_returns_all_records(self) -> None:
        transport = FakeTransport(
            [
                response({"code": 0, "tenant_access_token": "token", "expire": 7200}),
                response(
                    {
                        "code": 0,
                        "data": {
                            "items": [{"record_id": "1"}],
                            "has_more": True,
                            "page_token": "next",
                        },
                    }
                ),
                response(
                    {
                        "code": 0,
                        "data": {"items": [{"record_id": "2"}], "has_more": False},
                    }
                ),
            ]
        )
        service = BitableService(FeishuClient(self.feishu_settings, transport), "base")
        records = service.list_records("logs", page_size=100)
        self.assertEqual([item["record_id"] for item in records], ["1", "2"])
        self.assertEqual(transport.calls[2]["query"]["page_token"], "next")

    def test_bitable_search_pagination_returns_all_records(self) -> None:
        transport = FakeTransport(
            [
                response({"code": 0, "tenant_access_token": "token", "expire": 7200}),
                response(
                    {
                        "code": 0,
                        "data": {
                            "items": [{"record_id": "1"}],
                            "has_more": True,
                            "page_token": "next",
                        },
                    }
                ),
                response(
                    {
                        "code": 0,
                        "data": {"items": [{"record_id": "2"}], "has_more": False},
                    }
                ),
            ]
        )
        service = BitableService(FeishuClient(self.feishu_settings, transport), "base")
        records = service.search_all_records("logs", {"filter": {}})
        self.assertEqual([item["record_id"] for item in records], ["1", "2"])
        self.assertEqual(transport.calls[2]["query"]["page_token"], "next")

    def test_bitable_person_fields_use_open_id_mode(self) -> None:
        transport = FakeTransport(
            [
                response({"code": 0, "tenant_access_token": "token", "expire": 7200}),
                response(
                    {
                        "code": 0,
                        "data": {"records": [{"record_id": "rec1"}]},
                    }
                ),
            ]
        )
        service = BitableService(FeishuClient(self.feishu_settings, transport), "base")
        service.batch_create(
            "tbl",
            [{"报告人": [{"id": "ou_reporter"}]}],
        )
        self.assertEqual(transport.calls[1]["query"]["user_id_type"], "open_id")
        self.assertEqual(
            transport.calls[1]["json_body"]["records"][0]["fields"]["报告人"],
            [{"id": "ou_reporter"}],
        )

    def test_mobile_lookup_requests_open_id(self) -> None:
        transport = FakeTransport([
            response({"code": 0, "tenant_access_token": "token", "expire": 7200}),
            response({"code": 0, "data": {"user_list": [
                {"mobile": "13800000001", "user_id": "ou_person"}
            ]}}),
        ])
        contacts = ContactService(FeishuClient(self.feishu_settings, transport))
        users = contacts.batch_get_ids(mobiles=["13800000001"], user_id_type="open_id")
        self.assertEqual(users[0]["user_id"], "ou_person")
        self.assertEqual(transport.calls[1]["query"], {"user_id_type": "open_id"})
        self.assertEqual(transport.calls[1]["json_body"]["mobiles"], ["13800000001"])

    def test_document_creation_sends_folder_token_in_request_body(self) -> None:
        transport = FakeTransport([
            response({"code": 0, "tenant_access_token": "token", "expire": 7200}),
            response({"code": 0, "data": {"document": {"document_id": "doc1"}}}),
        ])
        docs = CloudDocsService(FeishuClient(self.feishu_settings, transport))
        self.assertEqual(docs.create_document("folder1", "小组日志"), "doc1")
        request = transport.calls[1]
        self.assertTrue(request["url"].endswith("/docx/v1/documents"))
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["json_body"],
                         {"folder_token": "folder1", "title": "小组日志"})
        self.assertIsNone(request["query"])

    def test_folder_locked_creation_is_retried(self) -> None:
        transport = FakeTransport([
            response({"code": 0, "tenant_access_token": "token", "expire": 7200}),
            response({"code": 1770036, "msg": "folder locked"}, status=400),
            response({"code": 0, "data": {"document": {"document_id": "doc1"}}}),
        ])
        docs = CloudDocsService(FeishuClient(self.feishu_settings, transport))
        with patch("tool.feishu.time.sleep") as sleep:
            self.assertEqual(docs.create_document("folder1", "小组日志"), "doc1")
        sleep.assert_called_once()
        self.assertEqual(len(transport.calls), 3)
        self.assertTrue(transport.calls[2]["url"].endswith("/docx/v1/documents"))

    def test_unexpected_creation_error_is_not_retried(self) -> None:
        transport = FakeTransport([
            response({"code": 0, "tenant_access_token": "token", "expire": 7200}),
            response({"code": 1061002, "msg": "params error"}, status=400),
        ])
        docs = CloudDocsService(FeishuClient(self.feishu_settings, transport))
        with patch("tool.feishu.time.sleep") as sleep:
            with self.assertRaises(FeishuApiError):
                docs.create_document("folder1", "小组日志")
        sleep.assert_not_called()
        self.assertEqual(len(transport.calls), 2)

    def test_deepseek_json_output_is_parsed_and_validated(self) -> None:
        transport = FakeTransport(
            [
                response(
                    {"choices": [{"message": {"content": '{"summary": "完成"}'}}]}
                )
            ]
        )
        client = DeepSeekClient(
            DeepSeekSettings(api_key="key", model="model"), transport
        )
        result = client.complete_json(
            [{"role": "user", "content": "请输出 json"}],
            validator=lambda value: value["summary"],
        )
        self.assertEqual(result, "完成")
        self.assertEqual(
            transport.calls[0]["json_body"]["response_format"],
            {"type": "json_object"},
        )

    def test_message_override_routes_workflow_messages_to_test_open_id(self) -> None:
        transport = FakeTransport(
            [
                response({"code": 0, "tenant_access_token": "token", "expire": 7200}),
                response({"code": 0, "data": {"message_id": "m1"}}),
            ]
        )
        messages = MessageService(
            FeishuClient(self.feishu_settings, transport),
            recipient_override="ou_test_receiver",
        )
        messages.send_text("ou_original_receiver", "test message")
        self.assertEqual(
            transport.calls[1]["json_body"]["receive_id"], "ou_test_receiver"
        )

    def test_default_schedule_file_is_valid(self) -> None:
        from workflow1.settings import W1Settings

        schedules = load_schedule_config("config/schedules.toml")
        self.assertEqual(schedules.timezone, "Asia/Shanghai")
        self.assertEqual(schedules.workflows["workflow1_daily"].time, "01:00")
        self.assertEqual(
            schedules.workflows["workflow1_daily"].options["material_days_ago"],
            1,
        )
        with patch.dict("os.environ", {"RECORDHUB_AUTO_ADVANCE_AT": "23:59"}):
            self.assertEqual(W1Settings.from_schedule(schedules).auto_advance_at,
                             schedules.workflows["workflow1_daily"].options[
                                 "auto_advance_at"])
        self.assertEqual(W1Settings.from_schedule(schedules).notify_at, "08:00")
        self.assertEqual(W1Settings.from_schedule(schedules).report_notify_at,
                         "22:00")
        # 阶段分析（S04）只汇总日志原文、不等人工评价定稿，所以凌晨就开跑，
        # 失败整点重试；08:00 才通知部长，当天 21:50 截止确认（过期按 AI 草稿定稿）。
        stage = schedules.workflows["s04_progress"]
        self.assertEqual(stage.time, "04:00")
        self.assertEqual(stage.options["notify_at"], "08:00")
        self.assertGreater(stage.options["notify_at"], stage.time)
        self.assertEqual(stage.options["confirmation_days"], 0)

    def test_schedule_file_accepts_utf8_with_and_without_bom(self) -> None:
        source = Path("config/schedules.toml").read_text(encoding="utf-8-sig")
        expected = load_schedule_config("config/schedules.toml")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schedules.toml"
            for encoding in ("utf-8", "utf-8-sig"):
                with self.subTest(encoding=encoding):
                    path.write_text(source, encoding=encoding)
                    self.assertEqual(load_schedule_config(path), expected)

    def test_invalid_schedule_syntax_includes_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.toml"
            path.write_text("!invalid", encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                load_schedule_config(path)
            self.assertIn(str(path), str(caught.exception))
            self.assertIn("line 1, column 1", str(caught.exception))
            self.assertIsNotNone(caught.exception.__cause__)

    def test_schedule_file_rejects_invalid_utf8_with_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.toml"
            path.write_bytes(b"\xff\xfeinvalid")
            with self.assertRaises(ValueError) as caught:
                load_schedule_config(path)
            self.assertIn(str(path), str(caught.exception))
            self.assertIsInstance(caught.exception.__cause__, UnicodeDecodeError)

    def test_invalid_confirmation_deadline_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.toml"
            path.write_text(
                'timezone = "Asia/Shanghai"\n'
                '[workflows.workflow1_daily]\n'
                'enabled = true\n'
                'schedule_type = "daily"\n'
                'time = "08:10"\n'
                'auto_advance_at = "25:00"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid auto_advance_at"):
                load_schedule_config(path)

    def test_invalid_schedule_time_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.toml"
            path.write_text(
                'timezone = "Asia/Shanghai"\n'
                '[workflows.bad]\n'
                'enabled = true\n'
                'schedule_type = "daily"\n'
                'time = "25:00"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "invalid time"):
                load_schedule_config(path)


class ScalarFieldTests(unittest.TestCase):
    """单选/人员/超链接字段同时带 id 和文字时，必须取到文字。"""

    def test_option_id_is_not_preferred_over_text(self) -> None:
        self.assertEqual(scalar([{"id": "optwbBg3XN", "text": "骨干学生"}]), "骨干学生")

    def test_person_field_falls_back_to_name(self) -> None:
        self.assertEqual(scalar([{"id": "ou_abc", "name": "江鑫鑫"}]), "江鑫鑫")

    def test_hyperlink_keeps_the_url(self) -> None:
        self.assertEqual(
            scalar({"link": "https://example.com/doc", "text": "阶段报告"}),
            "https://example.com/doc")

    def test_identifier_is_used_when_no_text_exists(self) -> None:
        self.assertEqual(scalar({"record_id": "rec_1"}), "rec_1")
        self.assertEqual(scalar([{"id": "optwbBg3XN"}]), "optwbBg3XN")

    def test_plain_values_are_unchanged(self) -> None:
        self.assertEqual(scalar("骨干学生"), "骨干学生")
        self.assertEqual(scalar(12), "12")
        self.assertEqual(scalar(None), "")
        self.assertEqual(scalar([]), "")


if __name__ == "__main__":
    unittest.main()

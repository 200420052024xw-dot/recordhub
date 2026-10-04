from __future__ import annotations

import json
import tempfile
import unittest
from collections import deque
from pathlib import Path
from typing import Any

from llm.client import DeepSeekClient
from config import DeepSeekSettings, FeishuSettings
from tool.feishu import BitableService, ContactService, FeishuClient, FeishuTokenProvider, MessageService
from tool.http import HttpResponse
from tool.notifications import NotificationService
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

    def test_pending_notification_uses_standard_text(self) -> None:
        transport = FakeTransport(
            [
                response({"code": 0, "tenant_access_token": "token", "expire": 7200}),
                response({"code": 0, "data": {"message_id": "m1"}}),
            ]
        )
        messages = MessageService(FeishuClient(self.feishu_settings, transport))
        result = NotificationService(messages).pending_confirmation(
            "user", title="昨日检查清单", record_url="https://example.test/record"
        )
        self.assertEqual(result["message_id"], "m1")
        content = json.loads(transport.calls[1]["json_body"]["content"])
        self.assertIn("待确认", content["text"])
        self.assertIn("https://example.test/record", content["text"])

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
        schedules = load_schedule_config("config/schedules.toml")
        self.assertEqual(schedules.timezone, "Asia/Shanghai")
        self.assertEqual(schedules.workflows["workflow1_daily"].time, "08:10")
        self.assertEqual(
            schedules.workflows["workflow1_daily"].options["material_days_ago"],
            1,
        )

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


if __name__ == "__main__":
    unittest.main()

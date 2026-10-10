from __future__ import annotations

import unittest

from data.repositories import OrganizationRepository

from datetime import date
from unittest.mock import Mock

from tool.errors import FeishuApiError, TransportError
from tool.feishu import MessageService
from schema import Department, Organization, Person
from service.runtime import make_recipient_resolver


class OrganizationMobileParsingTests(unittest.TestCase):
    def test_parse_organization_populates_mobile_and_open_id(self):
        person_fields = {
            "person_id": "人员编号",
            "name": "姓名",
            "role": "角色",
            "leader_ref": "直属上级",
            "minister_ref": "本部部长",
            "department_ref": "所属部门",
            "mobile": "手机号",
        }
        department_fields = {
            "department_id": "部门编号",
            "name": "部门名称",
            "minister_ref": "部门部长",
        }
        person_records = [{
            "record_id": "rec1",
            "fields": {
                "人员编号": "B1",
                "姓名": "杨天宇",
                "角色": "骨干学生",
                "所属部门": "杨阳蕊部门",
                "直属上级": [{"id": "ou_minister"}],
                "手机号": "+8615870637343",
            },
        }]
        departments, persons = OrganizationRepository._parse_organization(
            department_fields, [],
            person_fields, person_records,
            mobile_open_ids={"+8615870637343": "ou_b1"},
        )
        self.assertEqual(len(persons), 1)
        self.assertEqual(persons[0].mobile, "+8615870637343")
        self.assertEqual(persons[0].open_id, "ou_b1")


class MessageServiceLoggingTests(unittest.TestCase):
    def _service(self, resolver=None):
        client = Mock()
        return client, MessageService(client, recipient_resolver=resolver)

    def test_send_logs_success_with_recipient_info(self):
        client, messages = self._service(
            resolver=lambda oid: ("杨阳蕊", "+8613837176209"))
        client.request.return_value = {"code": 0, "data": {"message_id": "om_1"}}
        with self.assertLogs("tool.feishu", level="INFO") as captured:
            result = messages.send_text("ou_yyr", "hi")
        self.assertEqual(result, {"message_id": "om_1"})
        self.assertIn(
            "message_sent seq=1 type=text to=ou_yyr name=杨阳蕊 "
            "mobile=+8613837176209 message_id=om_1",
            captured.output[-1],
        )

    def test_send_logs_failure_and_reraises(self):
        client, messages = self._service()
        client.request.side_effect = FeishuApiError("boom", code=999)
        with self.assertLogs("tool.feishu", level="WARNING") as captured:
            with self.assertRaises(FeishuApiError):
                messages.send_text("ou_x", "hi")
        self.assertIn(
            "message_send_failed seq=1 type=text to=ou_x name=- mobile=- error=boom",
            captured.output[-1],
        )

    def test_sequence_increments_and_resets_per_day(self):
        client, messages = self._service()
        self.assertEqual(messages._next_seq(date(2026, 10, 8)), 1)
        self.assertEqual(messages._next_seq(date(2026, 10, 8)), 2)
        self.assertEqual(messages._next_seq(date(2026, 10, 9)), 1)

    def test_resolver_miss_logs_dash(self):
        client, messages = self._service(resolver=lambda oid: None)
        client.request.return_value = {"code": 0, "data": {"message_id": "om_2"}}
        with self.assertLogs("tool.feishu", level="INFO") as captured:
            messages.send_text("ou_unknown", "hi")
        self.assertIn("name=- mobile=-", captured.output[-1])

    def test_resolver_exception_logs_dash(self):
        def boom(oid):
            raise RuntimeError("nope")

        client, messages = self._service(resolver=boom)
        client.request.return_value = {"code": 0, "data": {"message_id": "om_3"}}
        with self.assertLogs("tool.feishu", level="INFO") as captured:
            messages.send_text("ou_bad", "hi")
        self.assertIn("name=- mobile=-", captured.output[-1])

    def test_send_logs_failure_on_transport_error_and_reraises(self):
        client, messages = self._service()
        client.request.side_effect = TransportError("network down")
        with self.assertLogs("tool.feishu", level="WARNING") as captured:
            with self.assertRaises(TransportError):
                messages.send_text("ou_x", "hi")
        self.assertIn(
            "message_send_failed seq=1 type=text to=ou_x name=- mobile=- error=network down",
            captured.output[-1],
        )

    def test_resolver_malformed_return_logs_dash(self):
        client, messages = self._service(resolver=lambda oid: ("only-name",))
        client.request.return_value = {"code": 0, "data": {"message_id": "om_4"}}
        with self.assertLogs("tool.feishu", level="INFO") as captured:
            messages.send_text("ou_bad", "hi")
        self.assertIn("name=- mobile=-", captured.output[-1])


class RecipientResolverWiringTests(unittest.TestCase):
    def test_make_recipient_resolver_maps_open_id_to_name_and_mobile(self):
        cache = Mock()
        cache.get.return_value = Organization(
            persons=[
                Person(person_id="B1", name="杨阳蕊", role="部长",
                       open_id="ou_yyr", mobile="+8613837176209"),
            ],
            departments=[Department(department_id="D1", name="杨阳蕊部门",
                                    minister_id="B1")],
            team_leader_id=None,
        )
        resolve = make_recipient_resolver(cache)
        self.assertEqual(resolve("ou_yyr"), ("杨阳蕊", "+8613837176209"))
        self.assertIsNone(resolve("ou_unknown"))

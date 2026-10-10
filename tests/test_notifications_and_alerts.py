"""Delivery regressions: historical suppression, manual API, and real error routing."""

import logging
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient
from config import AppSettings, DeepSeekSettings, FeishuSettings
from data import FileStateStore, utc_now
from service.alerts import AdminAlertHandler, install_admin_alerts
from service.api import create_app
from service.runtime import Runtime, WorkflowBinding
from tool.bitable_fields import SHANGHAI
from tool.diagnostics import DiagnosticFormatter, diagnostic_context, error_summary
from tool.errors import FeishuApiError, TransportError
from tool.feishu import FeishuClient
from tool.http import HttpResponse
from tool.notifications import NotificationPolicy, manual_notifications, capture_notifications
from workflow1.models import DailySnapshot, UnitStatus, WorkflowStatus
from workflow1.workflow import Workflow1
from workflow2.models import CycleRun
from workflow2.store import Workflow2Store
from workflow2.workflow import Workflow2
from test_workflow1_pipeline import sample_snapshot, DAY


class DeliveryPolicyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = folder.name
        self.policy = NotificationPolicy(self.root)
        self.day = utc_now().astimezone(SHANGHAI).date()
        self.send = Mock(return_value={"message_id": "msg"})

    def deliver(self, **changes):
        values = dict(key="key", target_date=self.day, send_date=self.day,
                      created_at=utc_now() + timedelta(seconds=1), receive_id="ou_member",
                      send=self.send, message="notice")
        values.update(changes)
        return self.policy.deliver(**values)

    def test_old_run_never_sends_automatically_even_after_restart(self):
        self.deliver(created_at=self.policy.activated_at - timedelta(days=1))
        self.policy = NotificationPolicy(self.root)
        self.deliver()
        self.send.assert_not_called()
        self.assertEqual(self.policy.for_date(self.day)[0]["status"], "suppressed")

    def test_new_run_for_old_date_is_not_a_backdoor_to_send(self):
        self.deliver(send_date=self.day - timedelta(days=1))
        self.send.assert_not_called()

    def test_failed_message_never_auto_retries_but_admin_can_send(self):
        self.send.side_effect = FeishuApiError("Fail", code=999, status_code=400)
        with self.assertRaises(FeishuApiError):
            self.deliver()
        self.send.side_effect = None
        self.policy = NotificationPolicy(self.root)
        self.deliver()
        self.assertEqual(self.send.call_count, 1)
        with manual_notifications(self.day) as results:
            self.deliver()
            self.deliver()
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual([item["status"] for item in results], ["sent", "already_sent"])
        saved = self.policy.for_date(self.day)[0]
        self.assertEqual(saved["attempts"], 2)
        self.assertEqual(saved["status"], "sent")

    def test_admin_permission_applies_only_to_selected_date(self):
        with manual_notifications(self.day - timedelta(days=1)):
            self.deliver(created_at=self.policy.activated_at)
        self.send.assert_not_called()

    def test_old_uncertain_delivery_requires_manual_check(self):
        self.send.side_effect = TransportError("response lost")
        with self.assertRaises(TransportError):
            self.deliver()
        state = self.policy._read()
        state["messages"]["key"]["updated_at"] = (utc_now() - timedelta(hours=2)).isoformat()
        FileStateStore._atomic_write(self.policy.path, state)
        self.send.side_effect = None
        with manual_notifications(self.day), self.assertRaisesRegex(ValueError, "人工核对"):
            self.deliver()
        self.assertEqual(self.send.call_count, 1)

    def test_concurrent_manual_calls_send_once(self):
        def call():
            with manual_notifications(self.day):
                return self.deliver()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: call(), range(2)))
        self.assertEqual(results, [{"message_id": "msg"}] * 2)
        self.send.assert_called_once()

    def test_payload_can_be_replayed_after_workflow_snapshot_cleanup(self):
        self.deliver(created_at=self.policy.activated_at, message="preserved report")
        messages = Mock()
        messages.send_text.return_value = {"message_id": "resent"}
        result = self.policy.replay(self.day, messages, workflow="workflow1")
        self.assertEqual(result["messages"][0]["status"], "sent")
        messages.send_text.assert_called_once_with("ou_member", "preserved report", idempotency_key="key")

    def test_capture_records_unattempted_message_even_for_new_run_without_sending(self):
        with capture_notifications(), manual_notifications(self.day):
            self.deliver()
        self.send.assert_not_called()
        self.assertEqual(self.policy.for_date(self.day)[0]["status"], "suppressed")

    def test_confirmed_unknown_may_be_retried_but_sent_message_is_still_skipped(self):
        self.send.side_effect = TransportError("lost response")
        with self.assertRaises(TransportError):
            self.deliver()
        state = self.policy._read()
        state["messages"]["key"]["updated_at"] = (utc_now() - timedelta(hours=2)).isoformat()
        FileStateStore._atomic_write(self.policy.path, state)
        self.send.side_effect = None
        with manual_notifications(self.day, confirm_unsent=True):
            self.deliver()
            self.deliver()
        self.assertEqual(self.send.call_count, 2)


class AlertTests(unittest.TestCase):
    def attach(self, messages):
        handler = AdminAlertHandler(messages, "ou_admin")
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(handler.close)
        self.addCleanup(root.removeHandler, handler)
        return handler

    def test_exception_alert_contains_context_type_traceback_and_provider_code(self):
        messages = Mock()
        messages.send_text.return_value = {"message_id": "alert"}
        handler = self.attach(messages)
        with diagnostic_context(target_date="2026-10-08", workflow="workflow1"):
            try:
                raise FeishuApiError("Fail", status_code=403, code=42,
                                     details={"ticket": "secret-ticket", "msg": "Fail"})
            except Exception:
                logging.getLogger("test.error").exception("failed phase=reconcile_human")
                # Propagation through another catch must not send a second alert.
                logging.getLogger("test.error").exception("same failure")
        handler.queue.join()
        messages.send_text.assert_called_once()
        recipient, content = messages.send_text.call_args.args
        self.assertEqual(recipient, "ou_admin")
        for expected in ("Traceback", "2026-10-08", "reconcile_human", "FeishuApiError", "code=42"):
            self.assertIn(expected, content)
        self.assertIn("中文说明=程序发生错误", content)
        self.assertIn("业务上下文=", content)
        self.assertIn("工作流", content)
        self.assertIn("错误类型说明=飞书开放接口返回错误", content)
        self.assertNotIn("secret-ticket", content)

    def test_alert_failure_is_logged_without_recursion(self):
        messages = Mock()
        def fail(*args, **kwargs):
            logging.getLogger("tool.feishu").error("message_send_failed")
            raise RuntimeError("admin unreachable")
        messages.send_text.side_effect = fail
        handler = self.attach(messages)
        with self.assertLogs("service.alerts", level="ERROR") as logs:
            logging.getLogger("test.error").error("workflow_failed")
            handler.queue.join()
        messages.send_text.assert_called_once()
        self.assertIn("admin_alert_delivery_failed", logs.output[0])

    def test_plain_warning_does_not_alert_but_warning_with_exception_does(self):
        messages = Mock()
        messages.send_text.return_value = {"message_id": "alert"}
        handler = self.attach(messages)
        logging.getLogger("test.error").warning("normal scheduler message")
        try:
            raise AssertionError()
        except Exception:
            logging.getLogger("test.error").warning("failed", exc_info=True)
        handler.queue.join()
        messages.send_text.assert_called_once()
        self.assertIn("AssertionError: (no message)", messages.send_text.call_args.args[1])

    def test_log_verbosity_does_not_disable_error_alerts(self):
        messages = Mock()
        messages.send_text.return_value = {"message_id": "alert"}
        root = logging.getLogger()
        previous_level = root.level
        root.setLevel(logging.CRITICAL)
        handler = install_admin_alerts(messages, "ou_admin")
        self.addCleanup(root.setLevel, previous_level)
        self.addCleanup(handler.close)
        self.addCleanup(root.removeHandler, handler)
        logging.getLogger("test.error").error("workflow_failed")
        handler.queue.join()
        messages.send_text.assert_called_once()


class DiagnosticTests(unittest.TestCase):
    def test_known_event_keeps_code_and_adds_chinese_explanation(self):
        record = logging.LogRecord("service.api", logging.ERROR, "", 1,
            "workflow1_finalize_failed date=2026-10-08", (), None)
        output = DiagnosticFormatter("%(message)s").format(record)
        self.assertIn("workflow1_finalize_failed", output)
        self.assertIn("中文说明=每日流程在截止收尾阶段失败", output)

    def test_redacts_sdk_url_and_authorization(self):
        record = logging.LogRecord("Lark", logging.INFO, "", 1,
            "connected wss://host?access_key=secret1&ticket=secret2 Bearer secret3", (), None)
        output = DiagnosticFormatter("%(message)s").format(record)
        self.assertNotIn("secret", output)
        self.assertIn("REDACTED", output)

    def test_empty_error_remains_actionable(self):
        self.assertEqual(error_summary(AssertionError()), "AssertionError: (no message)")

    def test_provider_failure_records_endpoint_and_request_id(self):
        transport = Mock()
        transport.request.return_value = HttpResponse(
            400, {"code": 123, "msg": "Fail"}, {"X-Tt-Logid": "request-abc"})
        token = Mock()
        token.get_token.return_value = "secret-token"
        client = FeishuClient(FeishuSettings("app", "secret", "base"), transport, token)
        with self.assertLogs("tool.feishu", level="ERROR") as logs:
            with self.assertRaises(FeishuApiError) as raised:
                client.request("GET", "bitable/v1/apps/app/tables/table/records")
        self.assertEqual(raised.exception.request_id, "request-abc")
        self.assertIn("code=123", logs.output[0])
        self.assertIn("request-abc", logs.output[0])
        self.assertNotIn("secret-token", logs.output[0])


class WorkflowNotificationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = folder.name
        self.store = FileStateStore(self.root, snapshot_model=DailySnapshot)
        self.store.get_or_create_workflow(DAY)
        self.snapshot = sample_snapshot()
        self.snapshot.evaluations_published = True
        for state in self.snapshot.log_evaluations.values():
            state.status = UnitStatus.WAITING_CONFIRMATION
        self.store.save_snapshot(self.snapshot)
        self.messages = Mock()
        self.messages.send_text.return_value = {"message_id": "msg"}
        self.workflow = Workflow1(
            store=self.store, organization_cache=Mock(), log_repository=Mock(),
            ai_evaluations=Mock(), human_evaluations=Mock(), prompt_service=Mock(),
            messages=self.messages, documents=Mock(), reports=Mock(),
            notifications=NotificationPolicy(self.root))

    def test_manual_send_bypasses_old_date_window_and_does_not_run_ai(self):
        self.workflow._notify_evaluators(DAY)
        self.messages.send_text.assert_not_called()
        result = self.workflow.send_notifications(DAY, "review")
        self.assertEqual({item["status"] for item in result["messages"]}, {"sent"})
        self.assertEqual(self.messages.send_text.call_count, 2)
        self.workflow.send_notifications(DAY, "review")
        self.assertEqual(self.messages.send_text.call_count, 2)
        self.workflow.prompt_service.execute.assert_not_called()
        self.workflow.documents.advance.assert_not_called()

    def test_one_failed_recipient_does_not_prevent_other_recipients(self):
        self.messages.send_text.side_effect = [FeishuApiError("Fail", code=1), {"message_id": "ok"}]
        result = self.workflow.send_notifications(DAY, "review")
        self.assertEqual({item["status"] for item in result["messages"]}, {"sent", "failed"})
        self.assertEqual(self.messages.send_text.call_count, 2)

    def test_missing_snapshot_preserves_original_error(self):
        day = DAY + timedelta(days=1)
        self.store.get_or_create_workflow(day)
        self.store.set_status(day, WorkflowStatus.FAILED, error="original startup error")
        result = self.workflow.finalize_pending_confirmations(target_date=day)
        self.assertEqual(result["status"], "not_ready")
        self.assertEqual(self.store.load_workflow(day).last_error, "original startup error")

    def test_admin_api_replays_after_snapshot_file_is_cleaned_up(self):
        self.workflow._notify_evaluators(DAY)
        self.store._workflow_path(DAY).unlink()
        settings = AppSettings(feishu=FeishuSettings("app", "secret", "base"),
            deepseek=DeepSeekSettings("key"), admin_token="admin-token",
            scheduler_enabled=False, event_stream_enabled=False)
        binding = WorkflowBinding("workflow1_daily", self.workflow, self.store, "19:00", "hook", {})
        runtime = Runtime(settings, Mock(messages=self.messages), Mock(), {"workflow1_daily": binding})
        client = TestClient(create_app(settings, runtime))
        response = client.post("/admin/notifications/send",
            headers={"Authorization": "Bearer admin-token"},
            json={"dates": [str(DAY)], "workflow": "workflow1", "message_type": "review"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"], {"sent": 2})
        self.assertEqual(self.messages.send_text.call_count, 2)

    def test_startup_capture_preserves_messages_without_sending(self):
        from service.runtime import capture_notification_backlog
        settings = AppSettings(feishu=FeishuSettings("app", "secret", "base"),
                               deepseek=DeepSeekSettings("key"))
        binding = WorkflowBinding("workflow1_daily", self.workflow, self.store, "19:00", "hook", {})
        runtime = Runtime(settings, Mock(messages=self.messages), Mock(), {"workflow1_daily": binding})
        capture_notification_backlog(runtime)
        self.messages.send_text.assert_not_called()
        self.assertEqual(len(self.workflow.notifications.for_date(DAY)), 2)
        self.workflow.prompt_service.execute.assert_not_called()

    def test_failure_after_confirmation_keeps_counts_and_phase(self):
        self.workflow._reconcile_human = Mock(return_value=3)
        self.workflow._close_remaining = Mock(return_value=4)
        self.workflow.advance = Mock(side_effect=FeishuApiError("Fail", code=42))
        result = self.workflow.finalize_pending_confirmations(DAY)
        self.assertEqual((result["human_confirmed"], result["auto_confirmed"]), (3, 4))
        self.assertIn("advance_documents", result["errors"][0]["stage"])
        self.assertIn("code=42", self.store.load_workflow(DAY).last_error)

    def test_periodic_old_notice_suppressed_and_manual_send_works(self):
        from config import load_schedule_config
        from schema import Organization, Person
        from uuid import uuid4
        store = Workflow2Store(self.root)
        run = CycleRun(run_id=str(uuid4()), kind="weekly", scheduled_date=DAY,
                       start_date=DAY - timedelta(days=7), end_date=DAY - timedelta(days=1),
                       created_at=utc_now() - timedelta(days=1), updated_at=utc_now(),
                       status="COMPLETED", document_urls={"S08": "https://8", "S09": "https://9"})
        store.save(run)
        org = Mock()
        org.get.return_value = Organization(
            persons=[Person(person_id="T", name="负责人", role="团队负责人", open_id="ou_t")],
            departments=[], team_leader_id="T")
        workflow = Workflow2(store=store, preparer=Mock(), organization=org,
            prompts=None, tables=Mock(), prompt_service=Mock(), messages=self.messages,
            documents=Mock(), schedules=load_schedule_config("config/schedules.toml"),
            archive_parent="folder", notifications=self.workflow.notifications)
        workflow._notify_leader(run, "ou_t")
        self.messages.send_text.assert_not_called()
        result = workflow.send_notifications(DAY, "report", "weekly")
        self.assertEqual(result["messages"][0]["status"], "sent")
        self.messages.send_text.assert_called_once()


class NotificationApiTests(unittest.TestCase):
    def setUp(self):
        self.settings = AppSettings(feishu=FeishuSettings("app", "secret", "base"),
            deepseek=DeepSeekSettings("key"), admin_token="admin-token",
            scheduler_enabled=False, event_stream_enabled=False)
        self.workflow = Mock()
        self.workflow.send_notifications.return_value = {"messages": [{"status": "sent"}]}
        self.binding = WorkflowBinding("workflow1_daily", self.workflow, Mock(), "19:00", "hook", {})
        self.runtime = Runtime(self.settings, Mock(), Mock(), {"workflow1_daily": self.binding})
        self.client = TestClient(create_app(self.settings, self.runtime), raise_server_exceptions=False)

    def test_send_requires_admin_token(self):
        response = self.client.post("/admin/notifications/send", json={"dates": [str(DAY)]})
        self.assertEqual(response.status_code, 401)
        self.workflow.send_notifications.assert_not_called()

    def test_multiple_dates_deduplicated_and_selected_message_type_passed(self):
        response = self.client.post("/admin/notifications/send",
            headers={"Authorization": "Bearer admin-token"},
            json={"dates": [str(DAY), str(DAY), str(DAY + timedelta(days=1))],
                  "workflow": "workflow1", "message_type": "report"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.workflow.send_notifications.call_count, 2)
        self.assertEqual(self.workflow.send_notifications.call_args_list[0].args, (DAY, "report"))

    def test_invalid_date_and_force_option_are_rejected(self):
        for payload in ({"dates": ["invalid"]}, {"dates": [str(DAY)], "force": True}):
            response = self.client.post("/admin/notifications/send",
                headers={"Authorization": "Bearer admin-token"}, json=payload)
            self.assertEqual(response.status_code, 422)
        self.workflow.send_notifications.assert_not_called()


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

from data.store import utc_now
from tool.bitable_fields import SHANGHAI
from tool.errors import FeishuApiError, TransportError
from tool.notifications import NotificationPolicy


class ReportRetryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.policy = NotificationPolicy(directory.name)
        self.policy.retry_callback = Mock()
        self.start = self.policy.activated_at + timedelta(seconds=1)
        # Keep every retry inside the same Shanghai calendar date.
        self.start = self.start.astimezone(SHANGHAI).replace(hour=12, minute=0)
        self.policy.activated_at = self.start - timedelta(seconds=1)
        self.day = self.start.date()
        self.messages = Mock()

    def deliver(self):
        return self.policy.deliver(key="report", target_date=self.day, created_at=self.start,
            send_date=self.day, receive_id="person", message="report body",
            send=lambda: self.messages.send_text("person", "report body", idempotency_key="report"))

    def test_definite_failure_retries_three_times_then_stops(self):
        self.messages.send_text.side_effect = FeishuApiError("rejected")
        with patch("tool.notifications.utc_now") as clock:
            clock.return_value = self.start
            with self.assertRaises(FeishuApiError):
                self.deliver()
            for attempt in range(1, 5):
                clock.return_value = self.start + timedelta(minutes=attempt * 10)
                self.policy.retry_report("report", self.messages)
        self.assertEqual(self.messages.send_text.call_count, 4)
        self.assertEqual(self.policy.retry_callback.call_count, 3)
        self.assertEqual(self.policy.for_date(self.day)[0]["attempts"], 4)

    def test_uncertain_result_never_gets_automatic_retry(self):
        self.messages.send_text.side_effect = TransportError("timeout")
        with patch("tool.notifications.utc_now", return_value=self.start):
            with self.assertRaises(TransportError):
                self.deliver()
            self.policy.retry_report("report", self.messages)
        self.assertEqual(self.messages.send_text.call_count, 1)
        self.policy.retry_callback.assert_not_called()

    def test_retry_does_not_cross_send_day(self):
        self.messages.send_text.side_effect = FeishuApiError("rejected")
        with patch("tool.notifications.utc_now") as clock:
            clock.return_value = self.start
            with self.assertRaises(FeishuApiError):
                self.deliver()
            clock.return_value = self.start + timedelta(days=1)
            self.policy.retry_report("report", self.messages)
        self.assertEqual(self.messages.send_text.call_count, 1)

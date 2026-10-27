"""Behavioral coverage for evidence, retention, durable delivery and AI fallback."""
from __future__ import annotations

import io
import json
import logging
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from config import AppSettings, DeepSeekSettings, FeishuSettings
from config.logs import RetainedJSONHandler, query_diagnostics
from service.alerts import AdminAlertHandler, DeepSeekAlertAnalyzer
from service.incidents import IncidentStore
from tool.diagnostics import diagnostic_context, execution_context, stage_diagnostics
from tool.errors import FeishuApiError
from tool.observability import (EventFilter, event_from_record, fingerprint,
                                install_record_factory, raw_error, recent_evidence)


def event(message="business_failed code=42", *, scope="2026-10-26", stage="write", now=None, level=logging.ERROR):
    record = logging.LogRecord("app.business", level, __file__, 1, message, (), None)
    record.diagnostic_snapshot = {"workflow": "workflow1", "target_date": scope,
        "run_id": scope, "execution_id": "execution-one", "stage": stage}
    if now is not None:
        record.created = now
    return event_from_record(record)


class EventTests(unittest.TestCase):
    def setUp(self):
        install_record_factory()

    def test_context_is_captured_before_another_thread_formats(self):
        with execution_context(workflow="workflow1", target_date="2026-10-26"):
            record = logging.getLogger("app").makeRecord("app", logging.ERROR, __file__, 1, "failed", (), None)
        with ThreadPoolExecutor(1) as pool:
            result = pool.submit(event_from_record, record).result()
        self.assertEqual(result["context"]["workflow"], "workflow1")
        self.assertNotEqual(result["execution_id"], "-")

    def test_parallel_executions_do_not_share_identifiers(self):
        def capture(_):
            with execution_context():
                record = logging.getLogger("app").makeRecord("app", logging.INFO, __file__, 1, "step", (), None)
                with execution_context():
                    inner = logging.getLogger("app").makeRecord("app", logging.INFO, __file__, 1, "inner", (), None)
                self.assertEqual(record.diagnostic_snapshot, inner.diagnostic_snapshot)
                return event_from_record(record)["execution_id"]
        with ThreadPoolExecutor(4) as pool:
            ids = list(pool.map(capture, range(12)))
        self.assertEqual(len(set(ids)), 12)

    def test_context_can_cross_a_worker_queue(self):
        with execution_context(workflow="workflow1"):
            context = copy_context()
            expected = logging.makeLogRecord({}).diagnostic_snapshot["execution_id"]
        with ThreadPoolExecutor(1) as pool:
            actual = pool.submit(context.run, lambda: logging.makeLogRecord({}).diagnostic_snapshot["execution_id"]).result()
        self.assertEqual(actual, expected)

    def test_sensitive_values_removed_but_names_retained(self):
        e = event("failed name=张三 mobile=13812345678 to=ou_private api_key=topsecret")
        encoded = json.dumps(e, ensure_ascii=False)
        for secret in ("13812345678", "ou_private", "topsecret"):
            self.assertNotIn(secret, encoded)
        self.assertIn("张三", encoded)

    def test_short_secret_does_not_disable_named_secret_redaction(self):
        install_record_factory(secrets=("key",))
        e = event("failed api_key=private")
        self.assertNotIn("private", json.dumps(e))
        install_record_factory()

    def test_token_usage_is_not_mistaken_for_a_secret(self):
        record = logging.LogRecord("llm", logging.INFO, __file__, 1, "usage", (), None)
        record.fields = {"prompt_tokens": 100, "completion_tokens": 20, "access_token": "private"}
        fields = event_from_record(record)["fields"]
        self.assertEqual(fields["prompt_tokens"], 100)
        self.assertEqual(fields["completion_tokens"], 20)
        self.assertEqual(fields["access_token"], "[REDACTED]")

    def test_exception_chain_and_provider_attributes_survive(self):
        try:
            try:
                raise FeishuApiError("denied", code=42, status_code=403)
            except FeishuApiError as exc:
                raise RuntimeError("write failed") from exc
        except RuntimeError:
            import sys
            record = logging.LogRecord("app", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
        e = event_from_record(record)
        self.assertEqual(e["exception"]["chain"][-1]["code"], 42)
        self.assertIn("RuntimeError: write failed", raw_error(e))
        self.assertIn("FeishuApiError: denied", raw_error(e))

    def test_evidence_is_byte_bounded_and_contains_current_event(self):
        with execution_context():
            for i in range(50):
                record = logging.getLogger("app").makeRecord("app", logging.INFO, __file__, 1,
                    "step " + "中" * 500, (), None)
                EventFilter().filter(record)
            current = event_from_record(record)
            evidence = recent_evidence(current)
        self.assertLessEqual(len(evidence), 40)
        self.assertLessEqual(len(json.dumps(evidence, ensure_ascii=False).encode()), 24576)
        self.assertEqual(evidence[-1]["event_id"], current["event_id"])

    def test_fingerprint_ignores_request_and_execution_ids(self):
        a = event("business_failed code=42 request_id=first")
        b = event("business_failed code=42 request_id=second")
        b["execution_id"] = "new-execution"
        self.assertEqual(fingerprint(a), fingerprint(b))
        self.assertNotEqual(fingerprint(a), fingerprint(event(scope="2026-10-25")))


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.now = time.time()
        self.handler = RetainedJSONHandler(self.root, clock=lambda: self.now, max_bytes=2000)
        self.addCleanup(self.handler.close)

    def write(self, name, items):
        path = self.root / name
        path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in items), encoding="utf-8")
        return path

    def test_cleanup_trims_expired_records_even_if_file_was_recently_modified(self):
        old = event("old", now=self.now - 72 * 3600 - 1)
        new = event("new", now=self.now - 60)
        path = self.write("recordhub.mixed.jsonl", [old, new])
        self.handler.cleanup()
        self.assertEqual([json.loads(line)["message"] for line in path.read_text(encoding="utf-8").splitlines()], ["new"])

    def test_expired_legacy_traceback_is_deleted_with_its_record(self):
        path = self.root / "recordhub.log"
        stamp = datetime.fromtimestamp(self.now - 4 * 86400).strftime("%Y-%m-%d %H:%M:%S,000")
        path.write_text(stamp + " ERROR original\nTraceback\nprivate detail\n", encoding="utf-8")
        self.handler.cleanup()
        self.assertFalse(path.exists())

    def test_rotation_and_capacity_do_not_touch_unrelated_files(self):
        unrelated = self.root / "business.json"
        unrelated.write_text("keep", encoding="utf-8")
        for _ in range(6):
            self.handler.handle(logging.LogRecord("app", logging.INFO, __file__, 1, "step " + "中" * 300, (), None))
        self.assertGreater(len(list(self.root.glob("*.jsonl"))), 1)
        self.handler.directory_max_bytes = 2500
        self.handler.cleanup()
        self.assertLessEqual(sum(p.stat().st_size for p in self.root.glob("*.jsonl")), 2500)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

    def test_diagnostics_finds_execution_and_excludes_expired_events(self):
        old = event("old", now=self.now - 4 * 86400)
        new = event("new", now=self.now)
        self.write("recordhub.query.jsonl", [old, new])
        settings = AppSettings(FeishuSettings("", "", ""), DeepSeekSettings(""), log_dir=self.folder.name)
        found = query_diagnostics(settings, event_id=new["event_id"])
        self.assertEqual([e["message"] for e in found["events"]], ["new"])
        self.assertEqual(query_diagnostics(settings, event_id=old["event_id"])["status"], "not_found")

    def test_cleanup_does_not_unlink_another_live_process_stream(self):
        other = RetainedJSONHandler(self.root, clock=lambda: self.now)
        self.addCleanup(other.close)
        other.handle(logging.LogRecord("other", logging.INFO, __file__, 1, "still live", (), None))
        self.handler.directory_max_bytes = 1
        self.handler.cleanup()
        self.assertTrue(other.active_path.exists())


class IncidentTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.now = time.time()
        self.path = Path(self.folder.name) / "alerts.sqlite3"
        self.store = self.connect()

    def connect(self):
        store = IncidentStore(self.path, clock=lambda: self.now)
        self.addCleanup(store.db.close)
        return store

    def test_restart_dedupes_and_six_hour_summary_counts_occurrences(self):
        e = event(now=self.now)
        self.store.record(e)
        other = self.connect()
        other.record(e)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM analyses").fetchone()[0], 1)
        self.now += 6 * 3600
        self.store.schedule_summaries()
        rows = self.store.db.execute("SELECT count FROM analyses ORDER BY created").fetchall()
        self.assertEqual([r[0] for r in rows], [1, 2])

    def test_two_connections_cannot_claim_the_same_analysis(self):
        self.store.record(event(now=self.now))
        other = self.connect()
        self.assertIsNotNone(self.store.claim_analysis("one", 15))
        self.assertIsNone(other.claim_analysis("two", 15))
        self.now += 46
        interrupted = other.claim_analysis("two", 15)
        self.assertEqual(interrupted["attempted"], 1)

    def test_urgent_escalation_bypasses_the_six_hour_window(self):
        ordinary = event(now=self.now)
        self.store.record(ordinary)
        self.now += 1
        urgent = event(now=self.now, level=logging.CRITICAL)
        self.store.record(urgent, urgent=True)
        item = self.store.claim_delivery("sender")
        self.assertIsNotNone(item)
        self.assertIn("CRITICAL", item["parts"])
        self.assertEqual(self.store.db.execute("SELECT status FROM analyses ORDER BY queued_at LIMIT 1").fetchone()[0], "cancelled")

    def test_expiry_deletes_all_originals_and_pending_notifications(self):
        self.store.record(event("unique-original-error", now=self.now), urgent=True)
        self.now += 72 * 3600 + 1
        self.store.cleanup()
        for table in ("incidents", "analyses", "outbox"):
            self.assertEqual(self.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        self.assertNotIn(b"unique-original-error", self.path.read_bytes())

    def test_unknown_delivery_stops_retrying_after_idempotency_window(self):
        self.store.record(event(now=self.now), urgent=True)
        item = self.store.claim_delivery("one")
        self.store.before_send(item, "one")
        self.store.delivery_failed(item, "one")
        self.now += 3601
        self.assertIsNone(self.store.claim_delivery("two"))
        self.assertEqual(self.store.db.execute("SELECT status FROM outbox").fetchone()[0], "unknown")

    def test_definite_rejection_can_retry_with_the_same_delivery_id(self):
        self.store.record(event(now=self.now), urgent=True)
        item = self.store.claim_delivery("one")
        self.store.before_send(item, "one")
        self.store.delivery_failed(item, "one", definite=True)
        self.now += 61
        retried = self.store.claim_delivery("two")
        self.assertEqual(item["id"], retried["id"])
        self.assertIsNone(retried["uncertain_since"])

    def test_raw_error_is_split_without_losing_unicode_or_tail(self):
        text = "中文原文" * 10000 + "THE-END"
        self.store.record(event(text, now=self.now), urgent=True)
        parts = json.loads(self.store.db.execute("SELECT parts FROM outbox").fetchone()[0])
        self.assertTrue(all(len(part.encode()) <= 8192 for part in parts))
        reconstructed = "".join(part.split("\n", 1)[1] for part in parts)
        self.assertIn(text, reconstructed)

    def test_only_explicit_matching_recovery_notifies_and_reopens_on_failure(self):
        e = event(now=self.now)
        self.store.record(e, urgent=True)
        item = self.store.claim_delivery("one")
        self.store.sent_part(item, "one", 1, True)
        self.store.recover(event(scope="another-date", now=self.now))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 1)
        self.store.recover(e)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 2)
        self.store.record(e)
        self.assertEqual(self.store.db.execute("SELECT resolved FROM incidents WHERE fingerprint IS NOT NULL").fetchone()[0], 0)


class AlertFlowTests(unittest.TestCase):
    def handler(self, analyzer=None, timeout=1):
        messages = Mock()
        messages.send_text.return_value = {"message_id": "sent"}
        handler = AdminAlertHandler(messages, "ou_admin", analyzer=analyzer, analysis_timeout=timeout)
        self.addCleanup(handler.close)
        return handler, messages

    def fire(self, handler, *, urgent=False):
        record = logging.LogRecord("app.business", logging.CRITICAL if urgent else logging.ERROR,
            __file__, 1, "original-error api_key=private", (), None)
        handler.emit(record)

    def test_success_calls_analyzer_once_and_sends_analysis(self):
        analyzer = Mock(return_value="模型分析结果")
        handler, messages = self.handler(analyzer)
        self.fire(handler)
        handler.queue.join()
        analyzer.assert_called_once()
        self.assertIn("模型分析结果", messages.send_text.call_args.args[1])
        self.fire(handler)
        handler.queue.join()
        analyzer.assert_called_once()
        messages.send_text.assert_called_once()

    def test_analysis_failure_sends_original_error_without_summary(self):
        handler, messages = self.handler(Mock(side_effect=ValueError("invalid JSON")))
        self.fire(handler)
        handler.queue.join()
        text = messages.send_text.call_args.args[1]
        self.assertIn("original-error", text)
        self.assertNotIn("private", text)
        self.assertNotIn("invalid JSON", text)

    def test_timeout_falls_back_and_discards_late_analysis(self):
        release = threading.Event()
        self.addCleanup(release.set)
        def analyze(item):
            release.wait(1)
            return "late result"
        handler, messages = self.handler(analyze, timeout=0.2)
        started = time.monotonic()
        self.fire(handler)
        handler.queue.join()
        self.assertLess(time.monotonic() - started, 0.8)
        self.assertIn("original-error", messages.send_text.call_args.args[1])
        release.set()
        messages.send_text.assert_called_once()

    def test_urgent_original_arrives_while_model_is_still_waiting(self):
        release, arrived = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def analyze(item):
            release.wait(1)
            return "analysis supplement"
        handler, messages = self.handler(analyze, timeout=0.5)
        messages.send_text.side_effect = lambda *args, **kwargs: (arrived.set() or {"message_id": "sent"})
        self.fire(handler, urgent=True)
        self.assertTrue(arrived.wait(0.4))
        self.assertIn("original-error", messages.send_text.call_args.args[1])
        release.set()
        handler.queue.join()
        self.assertEqual(messages.send_text.call_count, 2)

    def test_analyzer_and_sender_errors_do_not_recursively_alert(self):
        def analyze(item):
            logging.getLogger("tool.provider").error("analysis provider failure")
            raise ValueError("provider unavailable")
        handler, messages = self.handler(analyze)
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        messages.send_text.side_effect = FeishuApiError("denied", code=42)
        self.fire(handler)
        handler.queue.join()
        messages.send_text.assert_called_once()
        self.assertEqual(handler.store.db.execute("SELECT count(*) FROM incidents").fetchone()[0], 1)

    def test_interrupted_analysis_uses_original_without_calling_model_again(self):
        analyzer = Mock(return_value="must not be called")
        handler, messages = self.handler(analyzer)
        item = {"attempted": 1}
        self.assertIsNone(handler._analyze(item))
        analyzer.assert_not_called()

    def test_expired_queue_deadline_skips_model_and_uses_original(self):
        analyzer = Mock(return_value="must not be called")
        handler, _ = self.handler(analyzer)
        self.assertIsNone(handler._analyze({"attempted": 0, "queued_at": time.time() - 30}))
        analyzer.assert_not_called()

    def test_three_analysis_failures_open_the_circuit(self):
        analyzer = Mock(side_effect=RuntimeError("down"))
        handler, _ = self.handler(analyzer)
        for _ in range(4):
            self.assertIsNone(handler._analyze({"attempted": 0, "incident_id": "x"}))
        self.assertEqual(analyzer.call_count, 3)


class AnalyzerContractTests(unittest.TestCase):
    def item(self):
        e = event()
        return {"count": 1, "event": json.dumps(e), "evidence": json.dumps([e])}, e

    def test_rejects_fabricated_evidence_and_malformed_output(self):
        client = Mock()
        item, e = self.item()
        result = {"summary": "失败", "impact": "待确认", "possible_cause": "待确认",
            "suggestions": "检查权限", "uncertainty": "未确定根因", "evidence_ids": [e["event_id"]]}
        client.complete_json.side_effect = lambda *args, **kwargs: kwargs["validator"](result)
        analyzer = DeepSeekAlertAnalyzer(client)
        self.assertIn("AI 分析", analyzer(item))
        result["evidence_ids"] = ["invented"]
        with self.assertRaises(ValueError):
            analyzer(item)
        result.pop("impact")
        with self.assertRaises(ValueError):
            analyzer(item)


class EntryPointTests(unittest.TestCase):
    def settings(self, directory):
        return AppSettings(FeishuSettings("", "", ""), DeepSeekSettings(""), log_dir=directory,
                           scheduler_enabled=False, event_stream_enabled=False)

    def test_diagnostics_cli_does_not_start_clients_or_alerts(self):
        import main
        with tempfile.TemporaryDirectory() as folder:
            e = event(now=time.time())
            Path(folder, "recordhub.test.jsonl").write_text(json.dumps(e) + "\n", encoding="utf-8")
            output = io.StringIO()
            with patch("sys.argv", ["recordhub", "diagnostics", "--event-id", e["event_id"]]), \
                 patch.object(main.AppSettings, "from_env", return_value=self.settings(folder)), \
                 patch.object(main, "load_env_file"), patch.object(main, "setup_logging") as setup, \
                 patch("service.alerts.configure_admin_alerts") as configure, \
                 patch("service.alerts.close_admin_alerts"), patch("sys.stdout", output):
                self.assertEqual(main.main(), 0)
            setup.assert_not_called()
            configure.assert_not_called()
            self.assertEqual(json.loads(output.getvalue())["events"][0]["event_id"], e["event_id"])

    def test_http_success_and_exception_keep_the_response_execution_id(self):
        from fastapi.testclient import TestClient
        from service.api import create_app
        from service.runtime import Runtime, WorkflowBinding
        with tempfile.TemporaryDirectory() as folder:
            settings = self.settings(folder)
            binding = WorkflowBinding("workflow1_daily", Mock(), Mock(), "19:00", "hook", {})
            runtime = Runtime(settings, Mock(), Mock(), {"workflow1_daily": binding})
            app = create_app(settings, runtime)
            @app.get("/diagnostic-test-error")
            def failure():
                raise RuntimeError("test-only failure")
            install_record_factory()
            with self.assertLogs(level="INFO") as captured:
                client = TestClient(app, raise_server_exceptions=False)
                first = client.get("/health/live")
                second = client.get("/health/live")
                failed = client.get("/diagnostic-test-error")
                client.close()
            self.assertEqual(failed.status_code, 500)
            self.assertNotEqual(first.headers["X-RecordHub-Execution-ID"], second.headers["X-RecordHub-Execution-ID"])
            error = next(event_from_record(r) for r in captured.records if r.getMessage().startswith("http_request_failed"))
            self.assertEqual(error["execution_id"], failed.headers["X-RecordHub-Execution-ID"])

    def test_cli_failure_is_recorded_before_alerts_are_closed(self):
        import main
        order = []
        with patch.object(main, "_main", side_effect=RuntimeError("test")), \
             patch.object(main.logger, "exception", side_effect=lambda *a, **kw: order.append("record")), \
             patch("service.alerts.close_admin_alerts", side_effect=lambda: order.append("close")):
            with self.assertRaises(RuntimeError):
                main.main()
        self.assertEqual(order, ["record", "close"])


if __name__ == "__main__":
    unittest.main()

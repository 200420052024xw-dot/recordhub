import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch
import threading
from apscheduler.schedulers.background import BackgroundScheduler

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from data.store import utc_now
from service.execution import Appointments, TaskJournal, RecordedGeneration, scheduled_generation
from pydantic import BaseModel


class ModelOutput(BaseModel):
    text: str


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scheduler = Mock()

    def appointments(self, handler):
        tasks = Appointments(self.root, self.scheduler, handler)
        self.addCleanup(tasks.close)
        return tasks

    def test_persistent_unique_registration_and_single_claim(self):
        journal = TaskJournal(self.root)
        now = utc_now()
        journal.add("one", "generate", {}, now)
        reopened = TaskJournal(self.root)
        self.assertEqual(reopened.add("one", "different", {}, now)["action"], "generate")
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: reopened.claim("one"), range(8)))
        self.assertEqual(sum(item is not None for item in claims), 1)
        self.assertTrue((self.root / "execution" / "tasks.sqlite3").exists())

    def test_restore_releases_interrupted_work_but_not_success(self):
        tasks = self.appointments(Mock())
        tasks.book("running", "generate", {}, utc_now())
        tasks.journal.claim("running")
        tasks.book("done", "generate", {}, utc_now())
        tasks.execute("done")
        tasks.restore()
        self.assertEqual(tasks.journal.get("running")["status"], "running")
        tasks.restore(startup=True)
        self.assertEqual(tasks.journal.get("running")["status"], "pending")
        self.assertEqual(tasks.journal.get("done")["status"], "done")

    def test_due_first_attempt_precedes_retry_but_future_does_not_block(self):
        order = []
        tasks = self.appointments(lambda item: order.append(item["key"]))
        now = utc_now()
        tasks.book("retry", "generate", {}, now - timedelta(hours=2), lane="ai", retry=True)
        tasks.book("first", "generate", {}, now - timedelta(minutes=1), lane="ai")
        tasks.book("future", "generate", {}, now + timedelta(hours=1), lane="ai")
        tasks.drain_ai()
        self.assertEqual(order, ["first", "retry"])

    def test_failure_gets_only_three_hourly_retries(self):
        handler = Mock(side_effect=ValueError("unavailable"))
        tasks = self.appointments(handler)
        now = utc_now()
        tasks.book("one", "generate", {}, now)
        with patch("service.execution.utc_now") as clock:
            for attempt in range(4):
                clock.return_value = now + timedelta(hours=attempt)
                tasks.execute("one" if attempt == 0 else f"one#retry:{attempt}")
        self.assertEqual(handler.call_count, 4)
        self.assertIsNone(tasks.journal.get("one#retry:4"))
        self.assertTrue(all(t["ended_at"] for t in tasks.journal.all()))

    def test_cleanup_uses_end_time_and_preserves_active_work(self):
        tasks = self.appointments(Mock())
        now = utc_now()
        for key in ("old", "recent", "active"):
            tasks.book(key, "generate", {}, now - timedelta(days=10))
        tasks.execute("old")
        old = tasks.journal.get("old")
        old["ended_at"] = (now - timedelta(days=2)).isoformat()
        tasks.journal.save(old)
        tasks.execute("recent")
        tasks.journal.claim("active")
        self.assertEqual(tasks.journal.cleanup(now=now), 1)
        self.assertIsNone(tasks.journal.get("old"))
        self.assertEqual(tasks.journal.get("recent")["status"], "done")
        self.assertEqual(tasks.journal.get("active")["status"], "running")

    def test_expired_generation_never_calls_handler(self):
        handler = Mock()
        tasks = self.appointments(handler)
        tasks.book("old", "generate", {}, utc_now() - timedelta(hours=2),
                   expires=utc_now() - timedelta(hours=1))
        tasks.execute("old")
        handler.assert_not_called()
        self.assertEqual(tasks.journal.get("old")["status"], "expired")

    def test_real_scheduler_restores_due_timer_and_does_not_replay_success(self):
        first = self.appointments(Mock())
        first.book("one", "generate", {}, utc_now())
        first.journal.claim("one")
        first.close()
        scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        finished = threading.Event()
        calls = []
        def handle(task):
            calls.append(task["key"])
            finished.set()
        resumed = Appointments(self.root, scheduler, handle)
        try:
            resumed.restore(startup=True)
            scheduler.start()
            self.assertTrue(finished.wait(3))
            scheduler.pause()
            resumed.close()
            self.assertEqual(resumed.journal.get("one")["status"], "done")
            resumed.restore(startup=True)
            self.assertEqual(calls, ["one"])
        finally:
            resumed.close()
            scheduler.shutdown(wait=True)

    def test_control_timer_can_execute_while_ai_is_busy(self):
        entered, release, controlled = threading.Event(), threading.Event(), threading.Event()
        def handle(task):
            if task["lane"] == "ai":
                entered.set()
                release.wait(3)
            else:
                controlled.set()
        tasks = self.appointments(handle)
        tasks.book("ai", "generate", {}, utc_now(), lane="ai")
        tasks.book("close", "close", {}, utc_now())
        try:
            tasks.wake("ai")
            self.assertTrue(entered.wait(2))
            tasks.wake("close")
            self.assertTrue(controlled.wait(2))
        finally:
            release.set()

    def test_successful_model_output_survives_worker_restart(self):
        delegate = Mock()
        delegate.execute.return_value = ModelOutput(text="saved")
        kwargs = dict(input_data="source", prompt_code="test", template="prompt", output_model=ModelOutput)
        token = scheduled_generation.set(True)
        try:
            first = RecordedGeneration(delegate, self.root, lambda: False)
            first.context = "one"
            first.deadline = utc_now() + timedelta(hours=1)
            self.assertEqual(first.execute(**kwargs).text, "saved")
            restored = RecordedGeneration(delegate, self.root, lambda: True)
            restored.context = "one"
            self.assertEqual(restored.execute(**kwargs).text, "saved")
        finally:
            scheduled_generation.reset(token)
        delegate.execute.assert_called_once()

    def test_manual_model_call_is_not_interrupted_by_a_previous_appointment(self):
        delegate = Mock()
        generation = RecordedGeneration(delegate, self.root, lambda: True)
        generation.deadline = utc_now() - timedelta(hours=1)
        generation.execute(input_data="manual", prompt_code="test", template="prompt", output_model=ModelOutput)
        delegate.execute.assert_called_once()

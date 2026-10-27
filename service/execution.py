"""SQLite-backed appointments for the single RecordHub process."""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path

from data.store import FileStateStore, utc_now
from tool.diagnostics import execution_context

logger = logging.getLogger(__name__)
scheduled_generation = ContextVar("scheduled_generation", default=False)


class GenerationYield(BaseException):
    """A scheduling boundary, not a model or business failure."""


class RecordedGeneration:
    """Retain successful group/model results across interrupted phase retries."""
    def __init__(self, delegate, root, should_yield):
        self.delegate, self.root, self.should_yield = delegate, Path(root) / "execution" / "results", should_yield
        self.context = ""
        self.deadline = None

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def execute(self, **kwargs):
        # HTTP/CLI manual operations retain their synchronous behavior. Queue
        # control signals belong exclusively to an appointment's AI worker.
        if not scheduled_generation.get():
            return self.delegate.execute(**kwargs)
        value = kwargs["input_data"]
        value = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        signature = json.dumps([self.context, kwargs["prompt_code"], kwargs["template"], value,
                                kwargs["output_model"].__name__], ensure_ascii=False, sort_keys=True)
        path = self.root / (hashlib.sha256(signature.encode()).hexdigest() + ".json")
        if path.exists():
            return kwargs["output_model"].model_validate(FileStateStore._read_json(path)["result"])
        if (self.deadline and utc_now() >= self.deadline) or self.should_yield():
            raise GenerationYield()
        result = self.delegate.execute(**kwargs)
        if self.deadline and utc_now() >= self.deadline:
            raise GenerationYield()
        FileStateStore._atomic_write(path, {"result": result.model_dump(mode="json"),
            "context": self.context, "created_at": utc_now().isoformat(),
            "expires": self.deadline.isoformat() if self.deadline else None})
        return result


class TaskJournal:
    def __init__(self, root):
        self.root = Path(root) / "execution"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "tasks.sqlite3"
        self.lock = threading.RLock()
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tasks (
                key TEXT PRIMARY KEY, status TEXT NOT NULL, due TEXT NOT NULL,
                lane TEXT NOT NULL, retry INTEGER NOT NULL, ended_at TEXT,
                document TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS tasks_due ON tasks(status, lane, due)")

    @contextmanager
    def connect(self):
        # Short transactions and the default rollback journal keep this small.
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT document FROM tasks WHERE key = ?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def save(self, task):
        with self.lock, self.connect() as db:
            db.execute("""INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET status=excluded.status, due=excluded.due,
                lane=excluded.lane, retry=excluded.retry, ended_at=excluded.ended_at,
                document=excluded.document""",
                (task["key"], task["status"], task["due"], task["lane"], int(task["retry"]),
                 task.get("ended_at"), json.dumps(task, ensure_ascii=False)))

    def add(self, key, action, payload, when, *, lane="control", expires=None,
            retry=False, attempt=0):
        with self.lock:
            existing = self.get(key)
            if existing is not None:
                return existing
            task = dict(key=key, action=action, payload=payload, due=when.isoformat(),
                        lane=lane, expires=expires.isoformat() if expires else None,
                        retry=retry, attempt=attempt, status="pending", created_at=utc_now().isoformat())
            self.save(task)
            return task

    def all(self):
        with self.lock, self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT document FROM tasks")]

    def cleanup(self, retention_days=2, *, now=None):
        cutoff = ((now or utc_now()) - timedelta(days=retention_days)).isoformat()
        with self.lock, self.connect() as db:
            removed = db.execute("DELETE FROM tasks WHERE status IN ('done', 'failed', 'expired') "
                                 "AND ended_at <= ?", (cutoff,)).rowcount
        # Generated output stays available throughout its confirmation window,
        # then for two days. Active work with no deadline is retained.
        for path in (self.root / "results").glob("*.json"):
            item = FileStateStore._read_json(path)
            if item.get("expires") and datetime.fromisoformat(item["expires"]) <= datetime.fromisoformat(cutoff):
                path.unlink()
        return removed

    def claim(self, key):
        with self.lock:
            task = self.get(key)
            if task is None or task["status"] != "pending":
                return None
            if datetime.fromisoformat(task["due"]) > utc_now():
                return None
            task.update(status="running", started_at=utc_now().isoformat())
            self.save(task)
            return task


class Appointments:
    """The journal is authoritative; APScheduler's in-memory timers are derived.

    A single AI worker claims due tasks by priority. Control tasks have their
    own pool, so waiting/retrying generation cannot occupy a deadline timer.
    """
    def __init__(self, root, scheduler, handler, *, retry_minutes=60, retry_limit=3):
        self.journal = TaskJournal(root)
        self.scheduler, self.handler = scheduler, handler
        self.retry_minutes, self.retry_limit = retry_minutes, retry_limit
        self.ai_lock = threading.Lock()
        self.control = ThreadPoolExecutor(max_workers=4, thread_name_prefix="recordhub-control")
        self.ai = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recordhub-ai")
        self.closed = False

    def book(self, key, action, payload, when, **options):
        task = self.journal.add(key, action, payload, when, **options)
        self.arm(task)
        return task

    def arm(self, task):
        if task["status"] != "pending" or self.closed:
            return
        self.scheduler.add_job(self.wake, "date", args=[task["key"]],
                               run_date=max(datetime.fromisoformat(task["due"]), utc_now()),
                               id="appointment:" + task["key"], replace_existing=True,
                               misfire_grace_time=None, max_instances=1)

    def restore(self, *, startup=False):
        # Only startup can release interrupted claims: never duplicate live work.
        for task in self.journal.all():
            if startup and task["status"] == "running":
                task["status"] = "pending"
                self.journal.save(task)
            if task["status"] == "pending":
                self.arm(task)

    def wake(self, key):
        if self.closed:
            return
        task = self.journal.get(key)
        if task and task["status"] == "pending":
            if task["lane"] == "ai":
                self.ai.submit(self.drain_ai)
            else:
                self.control.submit(self.execute, key)

    def drain_ai(self):
        with self.ai_lock:
            while not self.closed:
                now = utc_now()
                tasks = [t for t in self.journal.all()
                         if t["status"] == "pending" and t["lane"] == "ai"
                         and datetime.fromisoformat(t["due"]) <= now]
                if not tasks:
                    return
                # First attempts precede retries, then nominal time and stable ID.
                task = min(tasks, key=lambda t: (t["retry"], datetime.fromisoformat(t["due"]), t["key"]))
                self.execute(task["key"])

    def has_first_attempt(self):
        return any(t["status"] == "pending" and t["lane"] == "ai" and not t["retry"]
                   and datetime.fromisoformat(t["due"]) <= utc_now() for t in self.journal.all())

    def execute(self, key):
        with execution_context(operation="appointment", appointment_key=key):
            return self._execute(key)

    def _execute(self, key):
        task = self.journal.claim(key)
        if task is None:
            return
        now = utc_now()
        if task["expires"] and now >= datetime.fromisoformat(task["expires"]):
            task.update(status="expired", ended_at=now.isoformat())
            self.journal.save(task)
            return
        logger.info("appointment_started key=%s action=%s attempt=%s reason=%s",
                    key, task["action"], task["attempt"], "retry" if task["retry"] else "scheduled")
        generation_token = scheduled_generation.set(task["lane"] == "ai")
        try:
            self.handler(task)
        except GenerationYield:
            # Resume saved work after giving newly due first attempts a turn.
            task["status"] = "pending"
            task["due"] = utc_now().isoformat()
            task["retry"] = True
            self.arm(task)
        except Exception as exc:
            task.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            delay = 10 if task["action"] == "delivery_retry" else self.retry_minutes
            when = utc_now() + timedelta(minutes=delay)
            expires = datetime.fromisoformat(task["expires"]) if task["expires"] else None
            if task["attempt"] < self.retry_limit and (expires is None or when < expires):
                self.book(key.split("#retry:")[0] + f"#retry:{task['attempt'] + 1}",
                          task["action"], task["payload"], when, lane=task["lane"],
                          expires=expires, retry=True, attempt=task["attempt"] + 1)
                logger.warning("appointment_retry_scheduled key=%s error=%s", key, task["error"])
            else:
                logger.exception("appointment_exhausted key=%s error=%s", key, task["error"],
                    extra={"fields": {"key": key.split("#retry:")[0]}})
        else:
            task["status"] = "done"
            logger.info("appointment_completed key=%s", key,
                extra={"fields": {"key": key.split("#retry:")[0]}, "alert": "recovery"})
        finally:
            scheduled_generation.reset(generation_token)
            if task["status"] in {"done", "failed", "expired"}:
                task["ended_at"] = utc_now().isoformat()
            self.journal.save(task)

    def close(self):
        self.closed = True
        self.ai.shutdown(wait=True)
        self.control.shutdown(wait=True)

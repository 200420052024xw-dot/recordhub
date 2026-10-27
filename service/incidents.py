"""Transactional incident aggregation and durable alert outbox (stdlib only)."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from datetime import datetime

from tool.observability import fingerprint, incident_scope, raw_error, recent_evidence


class IncidentStore:
    def __init__(self, path, *, retention_days=3, summary_minutes=360, clock=time.time):
        self.path = str(path)
        self.clock, self.retention_days = clock, retention_days
        self.interval = summary_minutes * 60
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # A single retained connection also makes in-memory stores useful in tests.
        import threading
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=2, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS incidents (
                id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE, scope TEXT NOT NULL,
                count INTEGER NOT NULL, reported_count INTEGER NOT NULL DEFAULT 0,
                urgent INTEGER NOT NULL DEFAULT 0,
                last_report REAL NOT NULL, updated REAL NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
                event TEXT, raw TEXT, evidence TEXT);
            CREATE TABLE IF NOT EXISTS analyses (
                id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, created REAL NOT NULL,
                queued_at REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL, attempted INTEGER NOT NULL DEFAULT 0,
                lease REAL NOT NULL DEFAULT 0, owner TEXT,
                event TEXT, raw TEXT, evidence TEXT, urgent INTEGER NOT NULL, count INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, created REAL NOT NULL,
                status TEXT NOT NULL, parts TEXT NOT NULL, next_part INTEGER NOT NULL DEFAULT 0,
                lease REAL NOT NULL DEFAULT 0, owner TEXT, retry_at REAL NOT NULL DEFAULT 0,
                uncertain_since REAL, error TEXT);
            CREATE INDEX IF NOT EXISTS outbox_due ON outbox(status, retry_at);
        """)
        if "queued_at" not in {row[1] for row in self.db.execute("PRAGMA table_info(analyses)")}:
            self.db.execute("ALTER TABLE analyses ADD COLUMN queued_at REAL NOT NULL DEFAULT 0")
            self.db.commit()
        if "urgent" not in {row[1] for row in self.db.execute("PRAGMA table_info(incidents)")}:
            self.db.execute("ALTER TABLE incidents ADD COLUMN urgent INTEGER NOT NULL DEFAULT 0")
            self.db.commit()
        self.cleanup()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def cleanup(self):
        cutoff = self.clock() - self.retention_days * 86400
        with self.transaction() as db:
            db.execute("DELETE FROM outbox WHERE created < ?", (cutoff,))
            db.execute("DELETE FROM analyses WHERE created < ?", (cutoff,))
            db.execute("DELETE FROM incidents WHERE updated < ?", (cutoff,))
            for item in db.execute("SELECT id,evidence FROM incidents WHERE evidence IS NOT NULL").fetchall():
                evidence = json.loads(item["evidence"])
                kept = [e for e in evidence if datetime.fromisoformat(e["timestamp"]).timestamp() >= cutoff]
                if len(kept) != len(evidence):
                    db.execute("UPDATE incidents SET evidence=? WHERE id=?", (json.dumps(kept, ensure_ascii=False), item["id"]))

    @staticmethod
    def _enqueue(db, incident_id, delivery_id, text, now):
        # Byte-based splitting preserves Unicode and the entire original error.
        chunks, chunk, size = [], [], 0
        for char in text:
            n = len(char.encode("utf-8"))
            if size + n > 7600:
                chunks.append("".join(chunk))
                chunk, size = [], 0
            chunk.append(char)
            size += n
        if chunk:
            chunks.append("".join(chunk))
        parts = [f"【RecordHub 告警】事故 {incident_id} ({i + 1}/{len(chunks)})\n{part}"
                 for i, part in enumerate(chunks)]
        db.execute("INSERT OR IGNORE INTO outbox(id,incident_id,created,status,parts) VALUES(?,?,?,'ready',?)",
                   (delivery_id, incident_id, now, json.dumps(parts, ensure_ascii=False)))

    def _round(self, db, incident, now, urgent):
        analysis_id = uuid4().hex
        # Copying evidence into a new notification never renews its retention.
        stamps = [datetime.fromisoformat(e["timestamp"]).timestamp() for e in json.loads(incident["evidence"])]
        source_time = min(stamps) if stamps else datetime.fromisoformat(json.loads(incident["event"])["timestamp"]).timestamp()
        db.execute("INSERT INTO analyses(id,incident_id,created,queued_at,status,event,raw,evidence,urgent,count) VALUES(?,?,?,?,'pending',?,?,?,?,?)",
                   (analysis_id, incident["id"], source_time, now, incident["event"], incident["raw"], incident["evidence"], int(urgent), incident["count"]))
        db.execute("UPDATE incidents SET last_report=?,reported_count=count WHERE id=?", (now, incident["id"]))
        if urgent:
            self._enqueue(db, incident["id"], analysis_id + ":raw", incident["raw"], source_time)
        return incident["id"]

    def record(self, event, *, urgent=False):
        now = self.clock()
        key = fingerprint(event)
        scope = json.dumps(incident_scope(event), sort_keys=True, ensure_ascii=False)
        encoded = json.dumps(event, ensure_ascii=False)
        evidence = json.dumps(recent_evidence(event, self.retention_days), ensure_ascii=False)
        with self.transaction() as db:
            old = db.execute("SELECT * FROM incidents WHERE fingerprint=?", (key,)).fetchone()
            if old and old["resolved"]:
                # A recurrence is a new incident, with its own delivery/recovery lifecycle.
                db.execute("UPDATE incidents SET fingerprint=NULL WHERE id=?", (old["id"],))
                old = None
            if old:
                db.execute("UPDATE incidents SET count=count+1,updated=?,event=?,raw=?,evidence=?,resolved=0,urgent=max(urgent,?) WHERE id=?",
                           (now, encoded, raw_error(event), evidence, int(urgent), old["id"]))
                item = db.execute("SELECT * FROM incidents WHERE id=?", (old["id"],)).fetchone()
                if urgent and not old["urgent"]:
                    db.execute("UPDATE analyses SET status='cancelled' WHERE incident_id=? AND status IN ('pending','running')", (old["id"],))
                    db.execute("UPDATE outbox SET status='cancelled' WHERE incident_id=? AND status='ready'", (old["id"],))
                    self._round(db, item, now, True)
                elif now - old["last_report"] >= self.interval:
                    self._round(db, item, now, urgent)
                return old["id"]
            incident_id = uuid4().hex
            db.execute("INSERT INTO incidents(id,fingerprint,scope,count,last_report,updated,event,raw,evidence,urgent) VALUES(?,?,?,1,?,?,?,?,?,?)",
                       (incident_id, key, scope, now, now, encoded, raw_error(event), evidence, int(urgent)))
            item = db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
            return self._round(db, item, now, urgent)

    def schedule_summaries(self):
        now = self.clock()
        with self.transaction() as db:
            for item in db.execute("SELECT * FROM incidents WHERE resolved=0 AND count>reported_count AND last_report<=? AND updated>=?",
                                   (now - self.interval, now - self.retention_days * 86400)).fetchall():
                self._round(db, item, now, False)

    def recover(self, event):
        scope = json.dumps(incident_scope(event), sort_keys=True, ensure_ascii=False)
        now = self.clock()
        with self.transaction() as db:
            for item in db.execute("SELECT * FROM incidents WHERE scope=? AND resolved=0", (scope,)).fetchall():
                # Only a delivered incident warrants a recovery message.
                delivered = db.execute("SELECT 1 FROM outbox WHERE incident_id=? AND status='sent'", (item["id"],)).fetchone()
                db.execute("UPDATE outbox SET status='cancelled' WHERE incident_id=? AND status IN ('ready','sending')", (item["id"],))
                if delivered:
                    self._enqueue(db, item["id"], uuid4().hex + ":recovered",
                                  f"【已恢复】对应业务范围已明确执行成功。\n执行编号：{event['execution_id']}\n事件编号：{event['event_id']}", now)
                # Don't send stale failure analyses after explicit recovery.
                db.execute("UPDATE analyses SET status='cancelled' WHERE incident_id=? AND status IN ('pending','running')", (item["id"],))
                db.execute("UPDATE incidents SET resolved=1 WHERE id=?", (item["id"],))

    def claim_analysis(self, owner, timeout):
        now = self.clock()
        with self.transaction() as db:
            item = db.execute("SELECT * FROM analyses WHERE status='pending' OR (status='running' AND lease<?) ORDER BY urgent DESC,created LIMIT 1", (now,)).fetchone()
            if item is None:
                return None
            db.execute("UPDATE analyses SET status='running',attempted=1,owner=?,lease=? WHERE id=?",
                       (owner, now + timeout + 30, item["id"]))
            return dict(item)

    def finish_analysis(self, item, owner, text):
        with self.transaction() as db:
            current = db.execute("SELECT * FROM analyses WHERE id=? AND owner=? AND status='running'", (item["id"], owner)).fetchone()
            if not current:
                return
            if text:
                self._enqueue(db, item["incident_id"], item["id"] + ":analysis", text, item["created"])
            elif not item["urgent"]:
                self._enqueue(db, item["incident_id"], item["id"] + ":raw", item["raw"], item["created"])
            db.execute("UPDATE analyses SET status='done',event=NULL,raw=NULL,evidence=NULL WHERE id=?", (item["id"],))

    def claim_delivery(self, owner):
        now = self.clock()
        with self.transaction() as db:
            # A transport loss must not resend outside Feishu's one-hour UUID window.
            db.execute("UPDATE outbox SET status='unknown',error='需要人工核对：超过上游去重窗口' WHERE status IN ('ready','sending') AND uncertain_since IS NOT NULL AND uncertain_since<=?",
                       (now - 3600,))
            item = db.execute("SELECT * FROM outbox WHERE (status='ready' AND retry_at<=?) OR (status='sending' AND lease<?) ORDER BY created,id LIMIT 1", (now, now)).fetchone()
            if item is None:
                return None
            db.execute("UPDATE outbox SET status='sending',owner=?,lease=? WHERE id=?", (owner, now + 90, item["id"]))
            return dict(item)

    def before_send(self, item, owner):
        with self.transaction() as db:
            return db.execute("UPDATE outbox SET uncertain_since=COALESCE(uncertain_since,?),lease=? WHERE id=? AND owner=? AND status='sending'",
                              (self.clock(), self.clock() + 90, item["id"], owner)).rowcount == 1

    def sent_part(self, item, owner, next_part, done):
        with self.transaction() as db:
            db.execute("UPDATE outbox SET next_part=?,status=?,uncertain_since=NULL,lease=? WHERE id=? AND owner=?",
                       (next_part, "sent" if done else "sending", self.clock() + 90, item["id"], owner))

    def delivery_failed(self, item, owner, *, definite=False):
        with self.transaction() as db:
            db.execute("UPDATE outbox SET status='ready',retry_at=?,lease=0,uncertain_since=CASE WHEN ? THEN NULL ELSE uncertain_since END,error=? WHERE id=? AND owner=?",
                       (self.clock() + 60, int(definite), "接口明确拒绝" if definite else "发送结果不明", item["id"], owner))

    def pending(self):
        with self.lock:
            return self.db.execute("SELECT (SELECT count(*) FROM analyses WHERE status IN ('pending','running')) + (SELECT count(*) FROM outbox WHERE status='sending' OR (status='ready' AND retry_at<=?))", (self.clock(),)).fetchone()[0]

    def release(self, owner):
        with self.transaction() as db:
            db.execute("UPDATE analyses SET lease=0 WHERE owner=? AND status='running'", (owner,))
            db.execute("UPDATE outbox SET lease=0 WHERE owner=? AND status='sending'", (owner,))

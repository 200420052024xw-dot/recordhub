"""JSONL diagnostics with age/capacity retention and readable stderr output."""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from uuid import uuid4

from config.settings import AppSettings
from tool.observability import ConsoleFormatter, EventFilter, JSONFormatter, install_record_factory, purge_recent

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def silence_third_party_console_handlers():
    for handler in list(logging.getLogger("Lark").handlers):
        logging.getLogger("Lark").removeHandler(handler)


def log_files(directory):
    """Only application-owned logs, never arbitrary files in the directory."""
    return sorted(p for p in Path(directory).glob("recordhub*")
                  if p.is_file() and (p.name.endswith(".jsonl") or p.name == "recordhub.log"
                                      or p.name.startswith("recordhub.log.")))


def _timestamp(line):
    try:
        return datetime.fromisoformat(json.loads(line)["timestamp"]).timestamp()
    except (ValueError, KeyError, TypeError):
        try:
            return datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f").timestamp()
        except ValueError:
            return None


def clean_file(path, cutoff):
    """Remove expired records; preserve a legacy traceback with its parent line."""
    temporary = path.with_name(path.name + f".{uuid4().hex}.tmp")
    try:
        with path.open(encoding="utf-8", errors="replace") as source:
            stamps = (_timestamp(line) for line in source)
            has_expired = any(stamp is not None and stamp < cutoff for stamp in stamps)
        if not has_expired and path.stat().st_mtime >= cutoff:
            return
        keep, wrote = False, False
        with path.open(encoding="utf-8", errors="replace") as source, temporary.open("w", encoding="utf-8") as target:
            for line in source:
                stamp = _timestamp(line)
                if stamp is not None:
                    keep = stamp >= cutoff
                elif path.suffix == ".jsonl":
                    keep = False
                if keep:
                    target.write(line)
                    wrote = True
        if wrote:
            os.replace(temporary, path)
        else:
            path.unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)


def _lease(path):
    """Nonblocking OS lock: a cleaner must never unlink another live stream."""
    handle = path.open("a+b")
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


class RetainedJSONHandler(TimedRotatingFileHandler):
    """Daily/size rotation plus startup/hourly cleanup, including idle periods."""
    def __init__(self, directory, *, retention_days=3, max_bytes=20 * 1024**2,
                 directory_max_bytes=1024**3, clock=time.time):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.retention_days, self.max_bytes = retention_days, max_bytes
        self.directory_max_bytes, self.clock = directory_max_bytes, clock
        self._stop = threading.Event()
        # Separate streams prevent CLI and service rotating each other's active file.
        self.active_path = self.directory / f"recordhub.{os.getpid()}.{uuid4().hex[:8]}.jsonl"
        self.lease_path = self.active_path.with_suffix(".jsonl.lease")
        self.lease = _lease(self.lease_path)
        super().__init__(self.active_path, when="midnight", backupCount=0, encoding="utf-8", delay=True)
        self.setFormatter(JSONFormatter())
        self.addFilter(EventFilter())
        self.cleanup()
        self._cleaner = threading.Thread(target=self._maintain, name="log-retention", daemon=True)
        self._cleaner.start()

    def shouldRollover(self, record):
        return super().shouldRollover(record) or (
            self.active_path.exists() and self.active_path.stat().st_size
            + len(self.format(record).encode("utf-8")) + 1 > self.max_bytes)

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        if self.active_path.exists():
            archive = self.directory / f"recordhub.{self.clock():.6f}.{uuid4().hex[:8]}.jsonl"
            self.active_path.rename(archive)
        self.rolloverAt = self.computeRollover(int(self.clock()))
        self.cleanup()

    def cleanup(self):
        self.acquire()
        try:
            if self.stream:
                self.stream.close()
                self.stream = None
            cutoff = self.clock() - self.retention_days * 86400
            purge_recent(self.retention_days, self.clock())
            for path in log_files(self.directory):
                lease = None
                try:
                    if path != self.active_path and path.with_suffix(".jsonl.lease").exists():
                        lease = _lease(path.with_suffix(".jsonl.lease"))
                        if lease is None:
                            continue
                    clean_file(path, cutoff)
                except OSError:
                    continue  # Another process can have the stream open on Windows.
                finally:
                    if lease:
                        lease.close()
                        if not path.exists():
                            path.with_suffix(".jsonl.lease").unlink(missing_ok=True)
            files = sorted(log_files(self.directory), key=lambda p: p.stat().st_mtime)
            total = sum(p.stat().st_size for p in files)
            for path in files:
                if total <= self.directory_max_bytes:
                    break
                try:
                    if path != self.active_path and path.with_suffix(".jsonl.lease").exists():
                        lease = _lease(path.with_suffix(".jsonl.lease"))
                        if lease is None:
                            continue
                        lease.close()
                    size = path.stat().st_size
                    path.unlink()
                    total -= size
                except OSError:
                    continue
        finally:
            self.release()

    def _maintain(self):
        while not self._stop.wait(3600):
            try:
                self.cleanup()
            except OSError as exc:
                sys.stderr.write(f"RecordHub log cleanup failed: {type(exc).__name__}\n")

    def close(self):
        self._stop.set()
        super().close()
        if self.lease:
            self.lease.close()
            self.lease = None
            self.lease_path.unlink(missing_ok=True)


def setup_logging(settings: AppSettings):
    level = logging.getLevelName(settings.log_level.upper())
    if not isinstance(level, int):
        raise ValueError(f"RECORDHUB_LOG_LEVEL 无效: {settings.log_level!r}")
    install_record_factory(settings.release, (settings.deepseek.api_key, settings.feishu.app_secret,
        settings.feishu.bitable_app_token, settings.admin_token, settings.workbuddy_token,
        settings.query_token, settings.review_token))
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.addFilter(EventFilter())
    console.setFormatter(ConsoleFormatter())
    file_handler = RetainedJSONHandler(settings.log_dir, retention_days=settings.log_retention_days,
        max_bytes=settings.log_max_bytes, directory_max_bytes=settings.log_directory_max_bytes)
    file_handler.setLevel(level)
    logging.basicConfig(level=level, handlers=[console, file_handler], force=True)
    silence_third_party_console_handlers()
    logging.getLogger("apscheduler").setLevel(logging.DEBUG if level <= logging.DEBUG else logging.WARNING)


def query_diagnostics(settings, *, event_id=None, execution_id=None):
    cutoff = time.time() - settings.log_retention_days * 86400
    def records():
        for path in log_files(settings.log_dir):
            if path.suffix != ".jsonl":
                continue
            try:
                with path.open(encoding="utf-8") as source:
                    for line in source:
                        try:
                            item = json.loads(line)
                            if datetime.fromisoformat(item["timestamp"]).timestamp() >= cutoff:
                                yield item
                        except (ValueError, KeyError, TypeError):
                            continue
            except OSError:
                continue
    if event_id:
        match = next((e for e in records() if e["event_id"] == event_id), None)
        if match is None:
            return {"status": "not_found", "detail": "资料已过期或不存在", "events": []}
        execution_id = match["execution_id"]
    matched = [e for e in records() if (execution_id != "-" and e["execution_id"] == execution_id)
               or e["event_id"] == event_id]
    matched.sort(key=lambda e: e["timestamp"])
    return {"status": "found" if matched else "not_found", "execution_id": execution_id,
            "detail": None if matched else "资料已过期或不存在", "events": matched}

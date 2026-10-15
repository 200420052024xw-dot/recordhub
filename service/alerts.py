"""Immediate asynchronous administrator alerts for application errors."""

import json
import logging
import queue
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from data import FileStateStore
from tool.diagnostics import DiagnosticFormatter

logger = logging.getLogger(__name__)


def _as_datetime(stamp) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None


class AdminAlertHandler(logging.Handler):
    # 同一个错误只打扰管理员一次。重试（周期任务的恢复轮询等）会反复抛同一条异常，
    # 全发出来会把真正的新问题淹掉，所以按"日志内容"去重。
    DEDUPE_LIMIT = 1000
    # 进程内去重挡不住"崩溃重启"这种循环：每次重启都是新 handler、空的记忆。
    # 2026-10-15 端口冲突时，1984 次重启发了 1988 条告警，把真正的问题埋了。
    # 所以同一指纹还要落盘记一份，窗口内不再重发。设为 0 即关闭跨进程抑制。
    SUPPRESS_MINUTES = 360

    def __init__(self, messages, admin_open_id: str, *, suppress_path=None,
                 suppress_minutes: int = SUPPRESS_MINUTES):
        super().__init__(logging.WARNING)
        self.messages, self.admin_open_id = messages, admin_open_id
        self.setFormatter(DiagnosticFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        self.queue = queue.Queue()
        self._seen: set[str] = set()
        self._seen_lock = threading.Lock()
        self._suppress_path = Path(suppress_path) if suppress_path else None
        self._suppress_minutes = suppress_minutes
        self._suppress_reported: set[str] = set()
        self.worker = threading.Thread(target=self._run, name="admin-alerts", daemon=True)
        self.worker.start()

    def emit(self, record):
        # Sending an alert can itself fail. Never recursively alert that failure.
        if threading.current_thread() is self.worker or record.name == __name__:
            return
        if record.levelno < logging.ERROR and not record.exc_info:
            return
        if record.exc_info and record.exc_info[1]:
            exc = record.exc_info[1]
            if getattr(exc, "_recordhub_alert_handler", None) is self:
                return
            exc._recordhub_alert_handler = self
        fingerprint = f"{record.name}|{record.levelno}|{record.getMessage()}"
        with self._seen_lock:
            if fingerprint in self._seen:
                return
            if len(self._seen) >= self.DEDUPE_LIMIT:
                # 只防重，不要求永久记忆；记满了就从头再来一轮。
                self._seen.clear()
            self._seen.add(fingerprint)
            if self._suppress_across_restarts(fingerprint):
                return
        event_id = uuid4().hex
        self.queue.put((event_id, self.format(record)))
        logger.info("admin_alert_queued event_id=%s source=%s", event_id, record.name)

    def _suppress_across_restarts(self, fingerprint: str) -> bool:
        """True when this exact alert already went out inside the window.

        Callers hold ``_seen_lock``. The memory on disk is what survives a
        restart loop; without it the administrator is paged once per restart.
        """
        if self._suppress_path is None or self._suppress_minutes <= 0:
            return False
        now = datetime.now(UTC)
        cutoff = now - timedelta(minutes=self._suppress_minutes)
        recent = {key: stamp for key, stamp in self._read_suppression().items()
                  if _as_datetime(stamp) is not None and _as_datetime(stamp) > cutoff}
        if fingerprint in recent:
            if fingerprint not in self._suppress_reported:
                # Say why the alerts stopped, once, instead of going silent.
                self._suppress_reported.add(fingerprint)
                logger.warning("admin_alert_suppressed minutes=%d fingerprint=%s",
                               self._suppress_minutes, fingerprint)
            return True
        recent[fingerprint] = now.isoformat()
        self._write_suppression(recent)
        return False

    def _read_suppression(self) -> dict[str, str]:
        if not self._suppress_path.exists():
            return {}
        try:
            raw = json.loads(self._suppress_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A damaged memory must never block alerts altogether.
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_suppression(self, entries: dict[str, str]) -> None:
        try:
            FileStateStore._atomic_write(self._suppress_path, entries)
        except OSError:
            logger.warning("admin_alert_suppression_not_saved path=%s", self._suppress_path)

    def _run(self):
        while True:
            item = self.queue.get()
            try:
                if item is None:
                    return
                event_id, detail = item
                result = self.messages.send_text(
                    self.admin_open_id,
                    f"【RecordHub 错误告警】事件 {event_id}\n{detail[:12000]}",
                    idempotency_key=f"admin-alert:{event_id}")
                if not result.get("message_id"):
                    raise ValueError("管理员告警未返回 message_id")
                logger.info("admin_alert_sent event_id=%s message_id=%s",
                            event_id, result["message_id"])
            except Exception:
                logger.exception("admin_alert_delivery_failed event_id=%s", item[0])
            finally:
                self.queue.task_done()

    def close(self):
        self.queue.put(None)
        if threading.current_thread() is not self.worker:
            self.worker.join(timeout=2)
        super().close()


def install_admin_alerts(messages, admin_open_id: str, *, suppress_path=None,
                         suppress_minutes: int = AdminAlertHandler.SUPPRESS_MINUTES):
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, AdminAlertHandler):
            root.removeHandler(handler)
            handler.close()
    if not admin_open_id:
        logger.error("admin_alerts_disabled reason=RECORDHUB_ADMIN_OPEN_ID_not_configured")
        return None
    handler = AdminAlertHandler(messages, admin_open_id, suppress_path=suppress_path,
                                suppress_minutes=suppress_minutes)
    # Logging verbosity must not disable the separate alert channel.
    if root.level > logging.WARNING:
        root.setLevel(logging.WARNING)
    root.addHandler(handler)
    logger.info("admin_alerts_enabled")
    return handler


def configure_admin_alerts(settings):
    from tool.feishu import FeishuClient, MessageService
    from tool.http import UrllibTransport
    # Independent client: admin alerts bypass business cutoffs and test recipient overrides.
    messages = MessageService(FeishuClient(
        settings.feishu, UrllibTransport(settings.http_timeout_seconds)))
    # The suppression memory has to outlive the process, or a restart loop
    # resets it and pages the administrator once per restart.
    return install_admin_alerts(
        messages, settings.admin_open_id,
        suppress_path=Path(settings.state_dir) / "admin_alerts.json")

"""Immediate asynchronous administrator alerts for application errors."""

import logging
import queue
import threading
from uuid import uuid4

from tool.diagnostics import DiagnosticFormatter

logger = logging.getLogger(__name__)


class AdminAlertHandler(logging.Handler):
    def __init__(self, messages, admin_open_id: str):
        super().__init__(logging.WARNING)
        self.messages, self.admin_open_id = messages, admin_open_id
        self.setFormatter(DiagnosticFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        self.queue = queue.Queue()
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
        event_id = uuid4().hex
        self.queue.put((event_id, self.format(record)))
        logger.info("admin_alert_queued event_id=%s source=%s", event_id, record.name)

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


def install_admin_alerts(messages, admin_open_id: str):
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, AdminAlertHandler):
            root.removeHandler(handler)
            handler.close()
    if not admin_open_id:
        logger.error("admin_alerts_disabled reason=RECORDHUB_ADMIN_OPEN_ID_not_configured")
        return None
    handler = AdminAlertHandler(messages, admin_open_id)
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
    return install_admin_alerts(messages, settings.admin_open_id)

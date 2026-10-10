"""Durable one-attempt business delivery and date-scoped admin sending."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from pathlib import Path
import logging
import threading

from data.store import FileStateStore, utc_now
from tool.bitable_fields import SHANGHAI
from tool.diagnostics import error_summary

logger = logging.getLogger(__name__)
_manual_date: ContextVar[date | None] = ContextVar("manual_notification_date", default=None)
_outcomes: ContextVar[list | None] = ContextVar("notification_outcomes", default=None)
_confirm_unsent: ContextVar[bool] = ContextVar("notification_confirm_unsent", default=False)
_capture: ContextVar[bool] = ContextVar("notification_capture", default=False)


@contextmanager
def capture_notifications():
    """Save prepared notifications on startup without sending any messages."""
    token = _capture.set(True)
    try:
        yield
    finally:
        _capture.reset(token)


def manual_delivery(target_date: date) -> bool:
    return _manual_date.get() == target_date


def confirmed_unsent(target_date: date) -> bool:
    return manual_delivery(target_date) and _confirm_unsent.get()


@contextmanager
def manual_notifications(target_date: date, confirm_unsent: bool = False):
    results: list[dict] = []
    date_token = _manual_date.set(target_date)
    result_token = _outcomes.set(results)
    confirmation_token = _confirm_unsent.set(confirm_unsent)
    try:
        yield results
    finally:
        _outcomes.reset(result_token)
        _confirm_unsent.reset(confirmation_token)
        _manual_date.reset(date_token)


def record_outcome(key: str, status: str, **details) -> None:
    results = _outcomes.get()
    if results is not None:
        results.append({"key": key, "status": status, **details})


class NotificationPolicy:
    """Persist activation and every attempt before doing remote work.

    Existing runs are frozen on first installation. Restarts retain that
    boundary; automatic recovery never retries a failed/uncertain delivery.
    """
    def __init__(self, root: str | Path):
        self.path = Path(root) / "notification_delivery.json"
        self.lock = threading.RLock()
        if not self.path.exists():
            FileStateStore._atomic_write(self.path, {
                "activated_at": utc_now().isoformat(), "messages": {}})
        self.activated_at = datetime.fromisoformat(self._read()["activated_at"])

    def _read(self) -> dict:
        return FileStateStore._read_json(self.path)

    def for_date(self, target_date: date) -> list[dict]:
        with self.lock:
            return [dict(item, key=key) for key, item in self._read()["messages"].items()
                    if item["target_date"] == target_date.isoformat()]

    def deliver(self, *, key: str, target_date: date, created_at: datetime,
                send_date: date, receive_id: str, send, message: str = "",
                workflow: str = "workflow1", message_type: str = "report", kind: str = "all"):
        manual = manual_delivery(target_date)
        # Hold the lock through the request so simultaneous admin/automatic
        # calls cannot both deliver the same key. The app uses one process.
        with self.lock:
            state = self._read()
            previous = state["messages"].get(key)
            if previous and previous["status"] == "sent":
                record_outcome(key, "already_sent", message_id=previous["message_id"])
                return {"message_id": previous["message_id"]}
            if _capture.get() or (not manual and (previous or created_at <= self.activated_at or
                                                  send_date < utc_now().astimezone(SHANGHAI).date())):
                if previous is None:
                    state["messages"][key] = {
                        "target_date": target_date.isoformat(), "receive_id": receive_id,
                        "message": message, "workflow": workflow,
                        "message_type": message_type, "kind": kind,
                        "status": "suppressed", "reason": "historical_notification",
                        "updated_at": utc_now().isoformat()}
                    FileStateStore._atomic_write(self.path, state)
                    logger.info("notification_suppressed date=%s key=%s reason=historical_notification",
                                target_date, key)
                return None
            if previous and previous["status"] == "attempting":
                elapsed = utc_now() - datetime.fromisoformat(previous["updated_at"])
                if elapsed >= timedelta(hours=1) and not confirmed_unsent(target_date):
                    raise ValueError(f"通知 {key} 发送结果未知且去重窗口已过，请先人工核对")
            item = {
                "target_date": target_date.isoformat(), "receive_id": receive_id,
                "message": message, "workflow": workflow,
                "message_type": message_type, "kind": kind,
                "status": "attempting", "manual": manual,
                "attempts": (previous or {}).get("attempts", 0) + 1,
                "updated_at": utc_now().isoformat()}
            state["messages"][key] = item
            FileStateStore._atomic_write(self.path, state)
            logger.info("notification_attempt date=%s key=%s to=%s manual=%s attempt=%d confirm_unsent=%s",
                        target_date, key, receive_id, manual, item["attempts"], confirmed_unsent(target_date))
            try:
                result = send()
                message_id = str(result.get("message_id", ""))
                if not message_id:
                    raise ValueError("飞书通知未返回 message_id，发送结果未知")
            except Exception as exc:
                # Preserve uncertainty for transport failures/crashes. Definite
                # API rejections may be retried explicitly by an administrator.
                from tool.errors import FeishuApiError
                item.update(status="failed" if isinstance(exc, FeishuApiError) else "attempting",
                            error=error_summary(exc))
                FileStateStore._atomic_write(self.path, state)
                record_outcome(key, "failed", error=item["error"])
                logger.exception("notification_failed date=%s key=%s to=%s manual=%s",
                                 target_date, key, receive_id, manual)
                raise
            item.update(status="sent", message_id=message_id,
                        updated_at=utc_now().isoformat())
            FileStateStore._atomic_write(self.path, state)
            record_outcome(key, "sent", message_id=message_id)
            logger.info("notification_delivered date=%s key=%s message_id=%s manual=%s",
                        target_date, key, message_id, manual)
            return result

    def replay(self, target_date: date, messages, *, workflow: str,
               message_type: str = "all", kind: str = "all", confirm_unsent: bool = False) -> dict | None:
        items = [item for item in self.for_date(target_date)
                 if item.get("workflow") == workflow and item.get("message") and
                 (message_type == "all" or item.get("message_type") == message_type) and
                 (kind == "all" or workflow == "workflow1" or item.get("kind") == kind)]
        if not items:
            return None
        with manual_notifications(target_date, confirm_unsent) as results:
            for item in items:
                key = item["key"]
                try:
                    self.deliver(
                        key=key, target_date=target_date, created_at=self.activated_at,
                        send_date=target_date, receive_id=item["receive_id"],
                        message=item["message"], workflow=workflow,
                        message_type=item["message_type"], kind=item.get("kind", "all"),
                        send=lambda item=item: messages.send_text(
                            item["receive_id"], item["message"], idempotency_key=item["key"]))
                except Exception as exc:
                    logger.exception("notification_replay_failed date=%s key=%s", target_date, key)
                    record_outcome(key, "failed", error=error_summary(exc))
            return {"workflow": workflow, "target_date": str(target_date),
                    "messages": list({item["key"]: item for item in results}.values())}

"""Feishu long connection: enqueue only; all reads happen on a worker."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import lark_oapi as lark

from config.logs import silence_third_party_console_handlers
from config.settings import AppSettings
from tool.feishu import BitableService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Job:
    table_id: str
    record_id: str


class BitableEventStream:
    """Routes registered Bitable record changes.

    Organization tables are never re-read on events; the cached copy is the
    runtime master data (see OrganizationCache) and refreshes happen only on
    startup or via the admin API.
    """

    def __init__(self, *, handlers: Mapping[str, Callable[[str], Any]],
                 bitable: BitableService,
                 settings: AppSettings) -> None:
        self.handlers = dict(handlers)
        self.bitable = bitable
        self.settings = settings
        self.queue: queue.Queue[_Job | None] = queue.Queue()
        self.pending: set[tuple[str, str]] = set()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None

    def start(self) -> None:
        # lark_oapi attaches its own stdout handler at import time; when this
        # module is imported after setup_logging we must strip it again.
        silence_third_party_console_handlers()
        self.bitable.subscribe_document_events()
        handler = (lark.EventDispatcherHandler.builder("", "")
            .register_p2_drive_file_bitable_record_changed_v1(self._on_record_changed)
            .build())
        client = lark.ws.Client(self.settings.feishu.app_id,
            self.settings.feishu.app_secret, event_handler=handler,
            log_level=lark.LogLevel.INFO)
        self.worker = threading.Thread(target=self._run_worker,
            name="feishu-event-worker", daemon=True)
        self.worker.start()
        threading.Thread(target=self._run_ws, args=(client,),
            name="feishu-event-stream", daemon=True).start()
        logger.info("feishu_event_stream_started app_token=%s tables=%d",
                    self.bitable.app_token, len(self.handlers))

    def stop(self) -> None:
        self.stop_event.set()
        self.queue.put(None)
        if self.worker:
            self.worker.join(timeout=10)

    def _on_record_changed(self, data: Any) -> None:
        try:
            event = getattr(data, "event", None)
            if event is None or event.file_token != self.bitable.app_token:
                return
            table_id = event.table_id or ""
            if table_id not in self.handlers:
                return
            for action in event.action_list or []:
                if action.action not in {"record_edited", "record_added"}:
                    continue
                record_id = action.record_id or ""
                key = (table_id, record_id)
                with self.lock:
                    if key in self.pending:
                        continue
                    self.pending.add(key)
                self.queue.put(_Job(table_id, record_id))
                logger.info("feishu_event_enqueued table_id=%s record_id=%s action=%s",
                            table_id, record_id, action.action)
        except Exception:
            logger.exception("feishu_event_callback_failed")

    @staticmethod
    def _run_ws(client: Any) -> None:
        logger.info("feishu_event_stream_connecting")
        try:
            client.start()
        except Exception:
            logger.exception("feishu_event_stream_failed")
        # client.start() is meant to block for the process lifetime; a return
        # means the long connection dropped and confirmation now relies on the
        # daily auto-advance until the service restarts.
        logger.warning("feishu_event_stream_loop_exited")

    def _run_worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                job = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                return
            try:
                self.handlers[job.table_id](job.record_id)
            except Exception:
                logger.exception("feishu_event_worker_failed")
            finally:
                with self.lock:
                    self.pending.discard((job.table_id, job.record_id))

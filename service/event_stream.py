"""Feishu long connection: enqueue only; all reads happen on a worker."""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any

import lark_oapi as lark

from config.settings import AppSettings
from tool.feishu import BitableService
from data.workflow1_evaluations import HumanEvaluationRepository
from workflow1.workflow import Workflow1

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Job:
    table_name: str
    record_id: str


class ConfirmationEventStream:
    """Routes questionnaire record changes only.

    Organization tables are never re-read on events; the cached copy is the
    runtime master data (see OrganizationCache) and refreshes happen only on
    startup or via the admin API.
    """

    def __init__(self, *, workflow: Workflow1, bitable: BitableService,
                 human_evaluations: HumanEvaluationRepository,
                 settings: AppSettings) -> None:
        self.workflow = workflow
        self.bitable = bitable
        self.human = human_evaluations
        self.settings = settings
        self.table_ids = {
            human_evaluations.table.table_id: "human_evaluations",
        }
        self.queue: queue.Queue[_Job | None] = queue.Queue()
        self.pending: set[tuple[str, str]] = set()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None

    def start(self) -> None:
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
            table_name = self.table_ids.get(event.table_id or "")
            if table_name is None:
                return
            for action in event.action_list or []:
                if action.action not in {"record_edited", "record_added"}:
                    continue
                record_id = action.record_id or ""
                key = (table_name, record_id)
                with self.lock:
                    if key in self.pending:
                        continue
                    self.pending.add(key)
                self.queue.put(_Job(table_name, record_id))
        except Exception:
            logger.exception("feishu_event_callback_failed")

    @staticmethod
    def _run_ws(client: Any) -> None:
        try:
            client.start()
        except Exception:
            logger.exception("feishu_event_stream_failed")

    def _run_worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                job = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                return
            try:
                self.workflow.handle_human_record(job.record_id)
            except Exception:
                logger.exception("feishu_event_worker_failed")
            finally:
                with self.lock:
                    self.pending.discard((job.table_name, job.record_id))

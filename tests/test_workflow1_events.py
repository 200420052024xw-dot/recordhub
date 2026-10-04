from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from service.bitable_events import BitableEventStream


class EventStreamTests(unittest.TestCase):
    def test_organization_tables_no_longer_trigger_any_refresh(self):
        workflow = Mock()
        bitable = Mock()
        bitable.app_token = "base"
        stream = BitableEventStream(
            handlers={"human": workflow.handle_human_record}, bitable=bitable,
            settings=Mock())
        for table_id, record_id in [("persons", "p1"), ("departments", "d1")]:
            event = SimpleNamespace(event=SimpleNamespace(
                file_token="base", table_id=table_id,
                action_list=[SimpleNamespace(action="record_edited",
                                             record_id=record_id)]))
            stream._on_record_changed(event)
        self.assertTrue(stream.queue.empty())
        stream.queue.put(None)
        stream._run_worker()
        workflow.handle_human_record.assert_not_called()

    def test_callback_only_queues_and_worker_routes_human_records(self):
        workflow = Mock()
        bitable = Mock()
        bitable.app_token = "base"
        stream = BitableEventStream(
            handlers={"human": workflow.handle_human_record}, bitable=bitable,
            settings=Mock())
        event = SimpleNamespace(event=SimpleNamespace(
            file_token="base", table_id="human",
            action_list=[SimpleNamespace(action="record_edited",
                                         record_id="h1")]))
        stream._on_record_changed(event)
        workflow.handle_human_record.assert_not_called()
        stream.queue.put(None)
        stream._run_worker()
        workflow.handle_human_record.assert_called_once_with("h1")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from service.event_stream import ConfirmationEventStream


class EventStreamTests(unittest.TestCase):
    def test_callback_only_queues_and_worker_routes_existing_tables(self):
        workflow = Mock()
        bitable = Mock()
        bitable.app_token = "base"
        human = Mock()
        human.table.table_id = "human"
        organization = Mock()
        organization.repository.config.tables = {
            "persons": SimpleNamespace(table_id="persons"),
            "departments": SimpleNamespace(table_id="departments"),
        }
        stream = ConfirmationEventStream(
            workflow=workflow, bitable=bitable, human_evaluations=human,
            organization_cache=organization, settings=Mock())
        for table_id, record_id in [("persons", "p1"), ("human", "h1")]:
            event = SimpleNamespace(event=SimpleNamespace(
                file_token="base", table_id=table_id,
                action_list=[SimpleNamespace(action="record_edited",
                                             record_id=record_id)]))
            stream._on_record_changed(event)
        organization.refresh.assert_not_called()
        workflow.handle_human_record.assert_not_called()
        stream.queue.put(None)
        stream._run_worker()
        organization.refresh.assert_called_once()
        workflow.handle_human_record.assert_called_once_with("h1")


if __name__ == "__main__":
    unittest.main()

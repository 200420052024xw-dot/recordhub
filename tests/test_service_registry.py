from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config import AppSettings, DeepSeekSettings, FeishuSettings
from config.schedules import ScheduleConfig, WorkflowSchedule
from service.api import _build_scheduler, _event_handlers, create_app
from service.runtime import Runtime, WorkflowBinding


def binding(name: str, table_id: str, *, auto_advance_at: str = "") -> WorkflowBinding:
    workflow = Mock()
    return WorkflowBinding(
        name=name,
        workflow=workflow,
        store=Mock(),
        auto_advance_at=auto_advance_at,
        confirmation_webhook_token="webhook-token",
        record_handlers={table_id: workflow.handle_human_record},
    )


class ServiceRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = AppSettings(
            feishu=FeishuSettings("app", "secret", "base"),
            deepseek=DeepSeekSettings("key"),
            admin_token="admin-token",
        )

    def test_api_routes_remain_registered_in_one_app(self) -> None:
        runtime = Runtime(
            settings=self.settings,
            infrastructure=Mock(),
            organization_cache=Mock(),
            workflows={"workflow1_daily": binding("workflow1_daily", "human")},
        )
        app = create_app(self.settings, runtime)
        paths = app.openapi()["paths"]
        self.assertIn("/webhooks/feishu/confirmations", paths)
        self.assertIn("/admin/workflows/daily/{target_date}", paths)
        self.assertIn("/admin/organization/persons", paths)

    def test_handlers_and_schedules_are_isolated_by_registration(self) -> None:
        first = binding("workflow1_daily", "human", auto_advance_at="12:00")
        second = binding("workflow2_daily", "checks")
        runtime = Runtime(
            settings=self.settings,
            infrastructure=Mock(),
            organization_cache=Mock(),
            workflows={first.name: first, second.name: second},
        )
        self.assertEqual(set(_event_handlers(runtime)), {"human", "checks"})
        schedules = ScheduleConfig(
            timezone="Asia/Shanghai",
            workflows={name: WorkflowSchedule(name, True, "daily", "08:10")
                       for name in runtime.workflows},
        )
        scheduler = _build_scheduler(schedules, runtime, self.settings)
        self.assertEqual(
            {job.id for job in scheduler.get_jobs()},
            {"workflow1_daily", "workflow2_daily",
             "workflow1_daily_confirmation_auto_advance", "snapshot_cleanup"},
        )

    def test_duplicate_table_registration_is_rejected(self) -> None:
        first = binding("workflow1_daily", "human")
        second = binding("workflow2_daily", "human")
        runtime = Runtime(
            settings=self.settings, infrastructure=Mock(),
            organization_cache=Mock(), workflows={first.name: first, second.name: second},
        )
        with self.assertRaisesRegex(ValueError, "Duplicate event handler"):
            _event_handlers(runtime)


if __name__ == "__main__":
    unittest.main()

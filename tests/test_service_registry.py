from __future__ import annotations

import asyncio
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from config import AppSettings, DeepSeekSettings, FeishuSettings
from config.schedules import ScheduleConfig, WorkflowSchedule, load_schedule_config
from service.api import _build_scheduler, _event_handlers, _make_lifespan, create_app
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

    def test_configured_notification_jobs_are_registered(self) -> None:
        settings = replace(self.settings, workflow2_enabled=True)
        runtime = Runtime(
            settings=settings, infrastructure=Mock(), organization_cache=Mock(),
            workflows={"workflow1_daily": binding("workflow1_daily", "human",
                                                  auto_advance_at="19:00")},
            workflow2=Mock(),
        )
        scheduler = _build_scheduler(
            load_schedule_config("config/schedules.toml"), runtime, settings)
        jobs = {job.id: str(job.trigger) for job in scheduler.get_jobs()}
        self.assertIn("hour='1'", jobs["workflow1_daily"])
        self.assertIn("hour='8'", jobs["workflow1_daily_evaluator_notification"])
        self.assertIn("workflow1_daily_evaluator_notification_recovery", jobs)
        self.assertIn("hour='19'", jobs["workflow1_daily_confirmation_auto_advance"])
        self.assertIn("hour='12'", jobs["workflow2_stage"])
        self.assertIn("hour='4'", jobs["workflow2_monthly"])
        self.assertIn("hour='12'", jobs["workflow2_monthly_notification"])
        self.assertIn("hour='6'", jobs["workflow2_weekly"])
        self.assertIn("hour='9'", jobs["workflow2_weekly_notification"])

    def test_duplicate_table_registration_is_rejected(self) -> None:
        first = binding("workflow1_daily", "human")
        second = binding("workflow2_daily", "human")
        runtime = Runtime(
            settings=self.settings, infrastructure=Mock(),
            organization_cache=Mock(), workflows={first.name: first, second.name: second},
        )
        with self.assertRaisesRegex(ValueError, "Duplicate event handler"):
            _event_handlers(runtime)

    def test_startup_records_activation_without_running_workflows(self) -> None:
        settings = replace(self.settings, workflow2_enabled=True,
                           scheduler_enabled=False, event_stream_enabled=False)
        daily = binding("workflow1_daily", "human")
        periodic = Mock()
        organization = Mock()
        organization.persons = []
        organization.departments = []
        cache = Mock()
        cache.refresh.return_value = organization
        runtime = Runtime(settings=settings, infrastructure=Mock(),
                          organization_cache=cache, workflows={daily.name: daily},
                          workflow2=periodic)
        schedules = load_schedule_config(
            Path(__file__).resolve().parents[1] / "config" / "schedules.toml")

        async def open_and_close() -> None:
            async with _make_lifespan(runtime, settings, schedules)(None):
                pass

        asyncio.run(open_and_close())
        daily.workflow.recover_incomplete.assert_not_called()
        periodic.recover.assert_not_called()
        periodic.catch_up.assert_not_called()
        periodic.store.activate.assert_called_once()


if __name__ == "__main__":
    unittest.main()

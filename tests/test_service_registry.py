from __future__ import annotations

import asyncio
import logging
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
from service.alerts import AdminAlertHandler


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
        self.assertIn("/admin/workflow2/retry", paths)
        self.assertIn("/admin/submission-statistics/{target_date}", paths)

    def test_statistics_endpoint_is_independent_and_requires_admin_token(self) -> None:
        from fastapi.testclient import TestClient
        daily = binding("workflow1_daily", "human")
        daily.workflow.refresh_submission_statistics.return_value = {"status": "complete", "written": 3}
        runtime = Runtime(settings=self.settings, infrastructure=Mock(), organization_cache=Mock(),
                          workflows={daily.name: daily})
        client = TestClient(create_app(self.settings, runtime))
        path = "/admin/submission-statistics/2026-10-08"
        self.assertEqual(client.post(path).status_code, 401)
        response = client.post(path, headers={"Authorization": "Bearer admin-token"})
        self.assertEqual(response.json(), {"status": "complete", "written": 3})
        daily.workflow.start.assert_not_called()
        daily.workflow.resume.assert_not_called()

    def test_admin_retry_endpoint_needs_the_admin_token(self) -> None:
        from fastapi.testclient import TestClient

        workflow2 = Mock()
        workflow2.retry_blocked.return_value = []
        runtime = Runtime(
            settings=self.settings, infrastructure=Mock(), organization_cache=Mock(),
            workflows={"workflow1_daily": binding("workflow1_daily", "human")},
            workflow2=workflow2,
        )
        client = TestClient(create_app(self.settings, runtime))

        self.assertEqual(client.post("/admin/workflow2/retry").status_code, 401)

        response = client.post("/admin/workflow2/retry?kind=stage",
                               headers={"Authorization": "Bearer admin-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"retried": []})
        workflow2.retry_blocked.assert_called_once_with("stage")

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
             "workflow1_daily_confirmation_auto_advance",
             "snapshot_cleanup", "organization_refresh"},
        )

    def test_failed_evaluation_retry_job_is_registered_for_workflow1(self) -> None:
        """接口失败的日志评价要有人定期去看有没有到点该重跑。"""
        from workflow1.workflow import Workflow1

        workflow = Workflow1.__new__(Workflow1)
        workflow.retry_failed_evaluations = Mock()
        workflow.refresh_submission_statistics = Mock()
        daily = WorkflowBinding(
            name="workflow1_daily", workflow=workflow, store=Mock(),
            auto_advance_at="", confirmation_webhook_token="token",
            record_handlers={},
        )
        runtime = Runtime(settings=self.settings, infrastructure=Mock(),
                          organization_cache=Mock(),
                          workflows={daily.name: daily})
        schedules = ScheduleConfig(
            timezone="Asia/Shanghai",
            workflows={daily.name: WorkflowSchedule(daily.name, True, "daily", "01:00")},
        )

        scheduler = _build_scheduler(schedules, runtime, self.settings)

        jobs = {job.id: job for job in scheduler.get_jobs()}
        self.assertIn("workflow1_daily_evaluation_retry", jobs)
        self.assertIn("workflow1_daily_statistics_retry", jobs)
        self.assertIn("hour='2,3,4'", str(jobs["workflow1_daily_statistics_retry"].trigger))
        jobs["workflow1_daily_statistics_retry"].func()
        workflow.refresh_submission_statistics.assert_called_once()
        daily.store.list_incomplete_workflows.assert_not_called()
        # 01:00 起跑 → 02:20 / 03:20 重试没跑完的当天，04:20 是最后一轮（标人工补评）
        self.assertIn("hour='2,3'", str(jobs["workflow1_daily_incomplete_retry"].trigger))
        self.assertIn("hour='4'", str(jobs["workflow1_daily_incomplete_retry_final"].trigger))

    def test_configured_notification_jobs_are_registered(self) -> None:
        settings = replace(self.settings, workflow2_enabled=True)
        runtime = Runtime(
            settings=settings, infrastructure=Mock(), organization_cache=Mock(),
            workflows={"workflow1_daily": binding("workflow1_daily", "human",
                                                  auto_advance_at="19:00")},
            workflow2=Mock(),
            prompt_repository=Mock(),
        )
        scheduler = _build_scheduler(
            load_schedule_config("config/schedules.toml"), runtime, settings)
        jobs = {job.id: str(job.trigger) for job in scheduler.get_jobs()}
        self.assertIn("hour='1'", jobs["workflow1_daily"])
        self.assertIn("hour='8'", jobs["workflow1_daily_evaluator_notification"])
        self.assertIn("hour='22'", jobs["workflow1_daily_report_notification"])
        self.assertNotIn("workflow1_daily_evaluator_notification_recovery", jobs)
        self.assertIn("hour='19'", jobs["workflow1_daily_confirmation_auto_advance"])
        # 阶段分析不等人工评价定稿（材料只带日志原文），所以凌晨就开跑；
        # 部长那边等 08:00 的通知。
        self.assertIn("hour='4'", jobs["workflow2_stage"])
        self.assertIn("hour='8'", jobs["workflow2_stage_notification"])
        # 22:00 才把定稿报告推给团队负责人
        self.assertIn("hour='22'", jobs["workflow2_stage_report_notification"])
        self.assertIn("hour='4'", jobs["workflow2_monthly"])
        self.assertIn("hour='12'", jobs["workflow2_monthly_notification"])
        self.assertIn("hour='6'", jobs["workflow2_weekly"])
        self.assertIn("hour='9'", jobs["workflow2_weekly_notification"])
        self.assertIn("minute='55'", jobs["skill_prompt_review"])

    def test_organization_refresh_failure_notifies_admin_and_continues(self) -> None:
        settings = replace(self.settings, admin_open_id="ou_admin")
        cache = Mock()
        cache.refresh.side_effect = RuntimeError("人员表读取失败")
        messages = Mock()
        messages.send_text.return_value = {"message_id": "alert"}
        alerts = AdminAlertHandler(messages, "ou_admin")
        logging.getLogger().addHandler(alerts)
        self.addCleanup(alerts.close)
        self.addCleanup(logging.getLogger().removeHandler, alerts)
        runtime = Runtime(
            settings=settings,
            infrastructure=Mock(messages=messages),
            organization_cache=cache,
            workflows={},
        )
        schedules = ScheduleConfig(timezone="Asia/Shanghai", workflows={})
        scheduler = _build_scheduler(schedules, runtime, settings)
        job = next(job for job in scheduler.get_jobs() if job.id == "organization_refresh")

        job.func()  # 刷新失败不应抛出，后续任务继续执行
        alerts.queue.join()

        cache.refresh.assert_called_once()
        messages.send_text.assert_called_once()
        self.assertEqual(messages.send_text.call_args.args[0], "ou_admin")
        self.assertIn("organization_refresh_failed", messages.send_text.call_args.args[1])
        self.assertIn("人员表读取失败", messages.send_text.call_args.args[1])

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

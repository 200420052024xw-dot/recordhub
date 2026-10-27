from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from scripts.deployment_check import inspect_deployment


class DeploymentCheckTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(__file__).resolve().parents[1]
        self.environment = {
            "FEISHU_APP_ID": "test-app",
            "FEISHU_APP_SECRET": "private-app-secret",
            "FEISHU_BITABLE_APP_TOKEN": "private-base-token",
            "DEEPSEEK_API_KEY": "private-llm-key",
            "RECORDHUB_ARCHIVE_PARENT_FOLDER_TOKEN": "private-folder-token",
            "RECORDHUB_ADMIN_OPEN_ID": "ou_test_admin",
            "RECORDHUB_ADMIN_TOKEN": "admin-" + "a" * 40,
            "RECORDHUB_WORKBUDDY_TOKEN": "submit-" + "b" * 40,
            "RECORDHUB_QUERY_TOKEN": "query-" + "c" * 40,
            "RECORDHUB_REVIEW_TOKEN": "review-" + "d" * 40,
            "RECORDHUB_TABLE_CONFIG": str(root / "config/tables.toml"),
            "RECORDHUB_SCHEDULE_CONFIG": str(root / "config/schedules.toml"),
            "RECORDHUB_STATE_DIR": self.directory,
            "RECORDHUB_LOG_DIR": self.directory,
            "RECORDHUB_RELEASE": "test-release",
        }
        self.enterContext(patch.dict(os.environ, self.environment, clear=True))
        self.enterContext(patch("config.load_env_file"))

    def test_offline_success_has_no_remote_or_alert_side_effects(self):
        with patch("tool.http.UrllibTransport.request") as remote, \
                patch("service.alerts.configure_admin_alerts") as alerts, \
                patch("service.runtime.build_runtime") as runtime:
            report = inspect_deployment(production=True)
        self.assertEqual(report["status"], "passed", report)
        remote.assert_not_called()
        alerts.assert_not_called()
        runtime.assert_not_called()
        self.assertEqual(list(Path(self.directory).iterdir()), [])
        self.assertNotIn(self.environment["FEISHU_APP_SECRET"], json.dumps(report))

    def test_production_allows_shared_http_token(self):
        os.environ["RECORDHUB_QUERY_TOKEN"] = os.environ["RECORDHUB_ADMIN_TOKEN"]
        report = inspect_deployment(production=True)
        self.assertEqual(report["status"], "passed", report)

    def test_simulation_cannot_pass_production_check(self):
        os.environ["RECORDHUB_SIMULATION_MODE"] = "true"
        os.environ["RECORDHUB_MESSAGE_OVERRIDE_OPEN_ID"] = "ou_simulation"
        self.assertIn("simulation mode is enabled",
                      inspect_deployment(production=True)["errors"])

    def test_invalid_setting_does_not_echo_secret_value(self):
        secret = "do-not-echo-this-private-value"
        os.environ["RECORDHUB_HTTP_TIMEOUT_SECONDS"] = secret
        report = inspect_deployment(production=True)
        self.assertEqual(report["status"], "blocked")
        self.assertNotIn(secret, json.dumps(report))

    def test_missing_runtime_directory_is_blocked(self):
        os.environ["RECORDHUB_STATE_DIR"] = str(Path(self.directory) / "missing")
        report = inspect_deployment(production=True)
        self.assertTrue(any("RECORDHUB_STATE_DIR" in item for item in report["errors"]))

    def test_port_occupied_by_another_listener_is_blocked(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            report = inspect_deployment(check_port=True, port=listener.getsockname()[1])
        self.assertTrue(any("cannot bind" in item for item in report["errors"]))

    def test_missing_prompt_is_blocked(self):
        with patch("scripts.deployment_check.Path.is_file", return_value=False):
            report = inspect_deployment(production=True)
        self.assertIn("missing prompt: S01.txt", report["errors"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from unittest.mock import patch

from config import AppSettings, DeepSeekSettings, FeishuSettings, setup_logging


def settings_for(directory: str, **overrides: object) -> AppSettings:
    values: dict = {
        "feishu": FeishuSettings("app", "secret", "base"),
        "deepseek": DeepSeekSettings("key"),
        "log_dir": directory,
    }
    values.update(overrides)
    return AppSettings(**values)  # type: ignore[arg-type]


class LoggingSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._directory.name)
        self.root = logging.getLogger()
        self.saved_handlers = self.root.handlers[:]
        self.saved_level = self.root.level

    def tearDown(self) -> None:
        # Close handlers before restoring so Windows releases the log file.
        for handler in self.root.handlers[:]:
            self.root.removeHandler(handler)
            handler.close()
        for handler in self.saved_handlers:
            self.root.addHandler(handler)
        self.root.setLevel(self.saved_level)
        self._directory.cleanup()

    def _log_file(self) -> Path:
        return self.log_dir / "recordhub.log"

    def _read_log(self) -> str:
        for handler in self.root.handlers:
            handler.flush()
        return self._log_file().read_text(encoding="utf-8")

    def test_root_gets_console_and_rotating_file(self) -> None:
        setup_logging(settings_for(str(self.log_dir)))
        console = [h for h in self.root.handlers
                   if isinstance(h, logging.StreamHandler)
                   and not isinstance(h, TimedRotatingFileHandler)]
        rotating = [h for h in self.root.handlers
                    if isinstance(h, TimedRotatingFileHandler)]
        self.assertEqual(len(self.root.handlers), 2)
        self.assertEqual(len(console), 1)
        self.assertEqual(len(rotating), 1)
        # stderr keeps stdout free for the CLI's JSON payloads.
        self.assertIs(console[0].stream, sys.stderr)
        handler = rotating[0]
        # 3 days = today's file + 2 midnight-rotated backups.
        self.assertEqual(handler.backupCount, 2)
        self.assertEqual(handler.when.upper(), "MIDNIGHT")
        self.assertEqual(handler.encoding, "utf-8")
        self.assertEqual(self.root.level, logging.INFO)

    def test_records_reach_file_including_third_party_loggers(self) -> None:
        setup_logging(settings_for(str(self.log_dir)))
        logging.getLogger("uvicorn.access").info("probe %s", 1)
        text = self._read_log()
        self.assertIn("probe 1", text)
        self.assertIn("uvicorn.access", text)
        self.assertIn("中文说明=HTTP 请求访问记录。", text)

    def test_application_event_includes_chinese_explanation(self) -> None:
        setup_logging(settings_for(str(self.log_dir)))
        logging.getLogger("service.api").error(
            "workflow1_finalize_failed date=%s stage=%s", "2026-10-08", "advance")
        text = self._read_log()
        self.assertIn("workflow1_finalize_failed", text)
        self.assertIn("中文说明=每日流程在截止收尾阶段失败", text)

    def test_setup_is_idempotent(self) -> None:
        setup_logging(settings_for(str(self.log_dir)))
        setup_logging(settings_for(str(self.log_dir)))
        self.assertEqual(len(self.root.handlers), 2)
        logging.getLogger("recordhub.probe").info("once %s", 1)
        self.assertEqual(self._read_log().count("once 1"), 1)

    def test_retention_one_keeps_no_backups(self) -> None:
        setup_logging(settings_for(str(self.log_dir), log_retention_days=1))
        handler = next(h for h in self.root.handlers
                       if isinstance(h, TimedRotatingFileHandler))
        self.assertEqual(handler.backupCount, 0)

    def test_invalid_level_rejected(self) -> None:
        with patch.dict(os.environ, {"RECORDHUB_LOG_LEVEL": "verbose"}):
            with self.assertRaises(ValueError):
                AppSettings.from_env()

    def test_lark_handler_detached(self) -> None:
        import lark_oapi  # noqa: F401  (installs its own stdout handler on "Lark")
        setup_logging(settings_for(str(self.log_dir)))
        self.assertEqual(logging.getLogger("Lark").handlers, [])
        # lark sets the "Lark" logger to WARNING at import; ws.Client later
        # raises it to INFO, so use WARNING here to match the effective level.
        logging.getLogger("Lark").warning("routed %s", 1)
        self.assertEqual(self._read_log().count("routed 1"), 1)


if __name__ == "__main__":
    unittest.main()

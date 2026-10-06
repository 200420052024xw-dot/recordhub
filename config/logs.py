"""Terminal + daily rotating file logging shared by serve and CLI commands."""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from config.settings import AppSettings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def silence_third_party_console_handlers() -> None:
    """Drop the stdout handler lark-oapi attaches to the shared "Lark" logger.

    lark_oapi.core.log installs it at import time while leaving propagation on,
    so once the root logger has handlers every Lark line prints twice on the
    terminal. The logger itself keeps propagating, so its records still reach
    the console and file through our single pair of handlers.
    """
    lark_logger = logging.getLogger("Lark")
    for handler in list(lark_logger.handlers):
        lark_logger.removeHandler(handler)


def setup_logging(settings: AppSettings) -> None:
    """Attach stderr + <log_dir>/recordhub.log handlers to the root logger.

    Idempotent: force=True replaces any handlers from a previous call.
    Retention semantics: N days = today's file + (N-1) midnight-rotated
    backups, so RECORDHUB_LOG_RETENTION_DAYS=3 keeps exactly three days.
    """
    level = logging.getLevelName(settings.log_level.upper())
    if not isinstance(level, int):
        raise ValueError(f"RECORDHUB_LOG_LEVEL 无效: {settings.log_level!r}")
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT)
    # stderr, not stdout: CLI subcommands print machine-readable JSON there.
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    file_handler = TimedRotatingFileHandler(
        log_dir / "recordhub.log",
        when="midnight",
        backupCount=max(settings.log_retention_days - 1, 0),
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=[console, file_handler], force=True)
    silence_third_party_console_handlers()

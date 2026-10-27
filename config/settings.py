"""Environment based configuration used by infrastructure tools."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path


def load_env_file(path: str | Path = ".env", *, override: bool = False) -> None:
    """Load a dependency-free .env file into the process environment."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and (override or key not in os.environ):
            os.environ[key] = value


@dataclass(frozen=True, slots=True)
class FeishuSettings:
    app_id: str
    app_secret: str
    bitable_app_token: str
    base_url: str = "https://open.feishu.cn/open-apis"


@dataclass(frozen=True, slots=True)
class DeepSeekSettings:
    api_key: str
    model: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com"


@dataclass(frozen=True, slots=True)
class AppSettings:
    feishu: FeishuSettings
    deepseek: DeepSeekSettings
    http_timeout_seconds: float = 30.0
    state_dir: str = "data/state"
    table_config_path: str = "config/tables.toml"
    schedule_config_path: str = "config/schedules.toml"
    admin_token: str = ""
    workbuddy_token: str = ""
    admin_open_id: str = ""
    message_override_open_id: str = ""
    simulation_mode: bool = False
    llm_concurrency: int = 3
    llm_max_attempts: int = 3
    # 日志评价遇到接口/网络失败时的延后重试：间隔多少分钟、最多几次。
    llm_retry_interval_minutes: int = 60
    llm_retry_limit: int = 3
    # 已完成快照保留天数；周期分析可以从飞书重建材料。
    snapshot_retention_days: int = 2
    # 未完成的日期：超过多少天发管理员告警、超过多少天连状态文件一起清理。
    # 已完成的状态按 snapshot_retention_days 回收，未完成的只能靠这两档兜底。
    stale_incomplete_alert_days: int = 3
    stale_incomplete_purge_days: int = 7
    scheduler_enabled: bool = True
    workflow2_enabled: bool = False
    event_stream_enabled: bool = True
    archive_parent_folder_token: str = ""
    log_dir: str = "logs"
    log_retention_days: int = 3
    log_level: str = "INFO"
    log_max_bytes: int = 20 * 1024 * 1024
    log_directory_max_bytes: int = 1024 * 1024 * 1024
    alert_ai_enabled: bool = True
    alert_analysis_timeout_seconds: int = 15
    alert_summary_minutes: int = 360
    release: str = "unknown"
    # 日志查询（WorkBuddy 查询口径）专用 token；留空时查询接口 503 不可用。
    query_token: str = ""
    # 批改（WorkBuddy 批改口径）专用 token；留空时批改接口 503 不可用。
    review_token: str = ""

    @classmethod
    def from_env(cls) -> "AppSettings":
        return cls(
            feishu=FeishuSettings(
                app_id=os.getenv("FEISHU_APP_ID", ""),
                app_secret=os.getenv("FEISHU_APP_SECRET", ""),
                bitable_app_token=os.getenv("FEISHU_BITABLE_APP_TOKEN", ""),
                base_url=os.getenv(
                    "FEISHU_BASE_URL", "https://open.feishu.cn/open-apis"
                ).rstrip("/"),
            ),
            deepseek=DeepSeekSettings(
                api_key=os.getenv("DEEPSEEK_API_KEY", ""),
                model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
                base_url=os.getenv(
                    "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
                ).rstrip("/"),
            ),
            http_timeout_seconds=float(
                os.getenv("RECORDHUB_HTTP_TIMEOUT_SECONDS", "30")
            ),
            state_dir=os.getenv("RECORDHUB_STATE_DIR", "data/state"),
            table_config_path=os.getenv(
                "RECORDHUB_TABLE_CONFIG", "config/tables.toml"
            ),
            schedule_config_path=os.getenv(
                "RECORDHUB_SCHEDULE_CONFIG", "config/schedules.toml"
            ),
            admin_token=os.getenv("RECORDHUB_ADMIN_TOKEN", ""),
            workbuddy_token=os.getenv("RECORDHUB_WORKBUDDY_TOKEN", "").strip(),
            admin_open_id=os.getenv("RECORDHUB_ADMIN_OPEN_ID", ""),
            message_override_open_id=os.getenv("RECORDHUB_MESSAGE_OVERRIDE_OPEN_ID", ""),
            simulation_mode=_boolean("RECORDHUB_SIMULATION_MODE", False),
            llm_concurrency=_positive_int("RECORDHUB_LLM_CONCURRENCY", 3),
            llm_max_attempts=_positive_int("RECORDHUB_LLM_MAX_ATTEMPTS", 3),
            llm_retry_interval_minutes=_positive_int(
                "RECORDHUB_LLM_RETRY_INTERVAL_MINUTES", 60
            ),
            llm_retry_limit=_positive_int("RECORDHUB_LLM_RETRY_LIMIT", 3),
            snapshot_retention_days=_positive_int(
                "RECORDHUB_SNAPSHOT_RETENTION_DAYS", 2
            ),
            stale_incomplete_alert_days=_positive_int(
                "RECORDHUB_STALE_INCOMPLETE_ALERT_DAYS", 3
            ),
            stale_incomplete_purge_days=_positive_int(
                "RECORDHUB_STALE_INCOMPLETE_PURGE_DAYS", 7
            ),
            scheduler_enabled=_boolean("RECORDHUB_SCHEDULER_ENABLED", True),
            workflow2_enabled=_boolean("RECORDHUB_WORKFLOW2_ENABLED", False),
            event_stream_enabled=_boolean("RECORDHUB_EVENT_STREAM_ENABLED", True),
            archive_parent_folder_token=os.getenv(
                "RECORDHUB_ARCHIVE_PARENT_FOLDER_TOKEN", "").strip(),
            log_dir=os.getenv("RECORDHUB_LOG_DIR", "logs").strip() or "logs",
            log_retention_days=_positive_int(
                "RECORDHUB_LOG_RETENTION_DAYS", 3
            ),
            log_level=_log_level("RECORDHUB_LOG_LEVEL", "INFO"),
            log_max_bytes=_positive_int("RECORDHUB_LOG_MAX_BYTES", 20 * 1024 * 1024),
            log_directory_max_bytes=_positive_int("RECORDHUB_LOG_DIRECTORY_MAX_BYTES", 1024 * 1024 * 1024),
            alert_ai_enabled=_boolean("RECORDHUB_ALERT_AI_ENABLED", True),
            alert_analysis_timeout_seconds=_positive_int("RECORDHUB_ALERT_ANALYSIS_TIMEOUT_SECONDS", 15),
            alert_summary_minutes=_positive_int("RECORDHUB_ALERT_SUMMARY_MINUTES", 360),
            release=os.getenv("RECORDHUB_RELEASE", "unknown").strip() or "unknown",
            query_token=os.getenv("RECORDHUB_QUERY_TOKEN", "").strip(),
            review_token=os.getenv("RECORDHUB_REVIEW_TOKEN", "").strip(),
        )

    def missing_variables(self) -> list[str]:
        required = {
            "FEISHU_APP_ID": self.feishu.app_id,
            "FEISHU_APP_SECRET": self.feishu.app_secret,
            "FEISHU_BITABLE_APP_TOKEN": self.feishu.bitable_app_token,
            "DEEPSEEK_API_KEY": self.deepseek.api_key,
            "DEEPSEEK_MODEL": self.deepseek.model,
            "RECORDHUB_ARCHIVE_PARENT_FOLDER_TOKEN": self.archive_parent_folder_token,
        }
        return [name for name, value in required.items() if not value]

    def validate(self) -> None:
        if self.simulation_mode and not self.message_override_open_id.strip():
            raise ValueError("RECORDHUB_SIMULATION_MODE requires RECORDHUB_MESSAGE_OVERRIDE_OPEN_ID")
        if self.stale_incomplete_purge_days <= self.stale_incomplete_alert_days:
            raise ValueError(
                "RECORDHUB_STALE_INCOMPLETE_PURGE_DAYS 必须大于 "
                "RECORDHUB_STALE_INCOMPLETE_ALERT_DAYS，否则状态文件会在"
                "管理员收到告警之前就被删除")
        missing = self.missing_variables()
        if missing:
            raise ValueError(f"Missing environment variables: {', '.join(missing)}")


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _log_level(name: str, default: str) -> str:
    value = os.getenv(name, default).strip().upper()
    if not isinstance(logging.getLevelName(value), int):
        raise ValueError(f"{name} must be one of DEBUG/INFO/WARNING/ERROR/CRITICAL")
    return value


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")

"""Environment based configuration used by infrastructure tools."""

from __future__ import annotations

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
    table_config_path: str = "config/feishu_tables.toml"
    schedule_config_path: str = "config/schedules.toml"
    confirmation_webhook_token: str = ""
    admin_token: str = ""
    admin_open_id: str = ""
    message_override_open_id: str = ""
    llm_concurrency: int = 3
    llm_max_attempts: int = 3
    external_max_attempts: int = 3
    snapshot_retention_days: int = 7
    scheduler_enabled: bool = True
    event_stream_enabled: bool = True
    auto_advance_at: str = "12:00"
    archive_parent_folder_token: str = ""

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
                "RECORDHUB_TABLE_CONFIG", "config/feishu_tables.toml"
            ),
            schedule_config_path=os.getenv(
                "RECORDHUB_SCHEDULE_CONFIG", "config/schedules.toml"
            ),
            confirmation_webhook_token=os.getenv(
                "RECORDHUB_CONFIRMATION_WEBHOOK_TOKEN", ""
            ),
            admin_token=os.getenv("RECORDHUB_ADMIN_TOKEN", ""),
            admin_open_id=os.getenv("RECORDHUB_ADMIN_OPEN_ID", ""),
            message_override_open_id=os.getenv("RECORDHUB_MESSAGE_OVERRIDE_OPEN_ID", ""),
            llm_concurrency=_positive_int("RECORDHUB_LLM_CONCURRENCY", 3),
            llm_max_attempts=_positive_int("RECORDHUB_LLM_MAX_ATTEMPTS", 3),
            external_max_attempts=_positive_int(
                "RECORDHUB_EXTERNAL_MAX_ATTEMPTS", 3
            ),
            snapshot_retention_days=_positive_int(
                "RECORDHUB_SNAPSHOT_RETENTION_DAYS", 7
            ),
            scheduler_enabled=_boolean("RECORDHUB_SCHEDULER_ENABLED", True),
            event_stream_enabled=_boolean("RECORDHUB_EVENT_STREAM_ENABLED", True),
            auto_advance_at=os.getenv("RECORDHUB_AUTO_ADVANCE_AT", "12:00").strip(),
            archive_parent_folder_token=os.getenv(
                "RECORDHUB_ARCHIVE_PARENT_FOLDER_TOKEN", "").strip(),
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
        missing = self.missing_variables()
        if missing:
            raise ValueError(f"Missing environment variables: {', '.join(missing)}")


def _positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
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

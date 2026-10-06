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
    admin_open_id: str = ""
    message_override_open_id: str = ""
    simulation_mode: bool = False
    llm_concurrency: int = 3
    llm_max_attempts: int = 3
    snapshot_retention_days: int = 3
    scheduler_enabled: bool = True
    workflow2_enabled: bool = False
    skill_config_path: str = "config/skill_versions.json"
    team_skill_department_id: str = "TEAM_MANAGEMENT"
    event_stream_enabled: bool = True
    archive_parent_folder_token: str = ""
    log_dir: str = "logs"
    log_retention_days: int = 3
    log_level: str = "INFO"

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
            admin_open_id=os.getenv("RECORDHUB_ADMIN_OPEN_ID", ""),
            message_override_open_id=os.getenv("RECORDHUB_MESSAGE_OVERRIDE_OPEN_ID", ""),
            simulation_mode=_boolean("RECORDHUB_SIMULATION_MODE", False),
            llm_concurrency=_positive_int("RECORDHUB_LLM_CONCURRENCY", 3),
            llm_max_attempts=_positive_int("RECORDHUB_LLM_MAX_ATTEMPTS", 3),
            snapshot_retention_days=_positive_int(
                "RECORDHUB_SNAPSHOT_RETENTION_DAYS", 3
            ),
            scheduler_enabled=_boolean("RECORDHUB_SCHEDULER_ENABLED", True),
            workflow2_enabled=_boolean("RECORDHUB_WORKFLOW2_ENABLED", False),
            skill_config_path=os.getenv("RECORDHUB_SKILL_CONFIG", "config/skill_versions.json"),
            team_skill_department_id=os.getenv("RECORDHUB_TEAM_SKILL_DEPARTMENT_ID", "TEAM_MANAGEMENT").strip(),
            event_stream_enabled=_boolean("RECORDHUB_EVENT_STREAM_ENABLED", True),
            archive_parent_folder_token=os.getenv(
                "RECORDHUB_ARCHIVE_PARENT_FOLDER_TOKEN", "").strip(),
            log_dir=os.getenv("RECORDHUB_LOG_DIR", "logs").strip() or "logs",
            log_retention_days=_positive_int(
                "RECORDHUB_LOG_RETENTION_DAYS", 3
            ),
            log_level=_log_level("RECORDHUB_LOG_LEVEL", "INFO"),
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
        if not self.team_skill_department_id.strip():
            raise ValueError("团队 Skill 配置归属编号不能为空")
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

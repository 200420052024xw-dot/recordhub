"""Application configuration public API."""

from config.schedules import ScheduleConfig, WorkflowSchedule, load_schedule_config
from config.settings import (
    AppSettings,
    DeepSeekSettings,
    FeishuSettings,
    load_env_file,
)
__all__ = [
    "AppSettings",
    "DeepSeekSettings",
    "FeishuSettings",
    "ScheduleConfig",
    "WorkflowSchedule",
    "load_env_file",
    "load_schedule_config",
    "load_table_config",
]


def load_table_config(path):
    """Load table mappings lazily to keep config usable without business deps."""
    from config.tables import load_table_config as loader

    return loader(path)

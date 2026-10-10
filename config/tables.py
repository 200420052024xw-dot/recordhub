from __future__ import annotations

import tomllib
from pathlib import Path

from schema import TableConfig


REQUIRED_TABLES = {
    "departments",
    "persons",
    "logs",
    "evaluations",
    "human_evaluations",
    "reports",
    "prompts",
}

REQUIRED_FIELDS = {
    "departments": {
        "department_id",
        "name",
        "minister_ref",
        "backbone_refs",
    },
    "persons": {
        "person_id",
        "name",
        "role",
        "leader_ref",
        "minister_ref",
        "department_ref",
        "remark",
        "mobile",
    },
    "logs": {
        "log_id",
        "submitted_at",
        "submitter_ref",
        "submitter_name",
        "progress",
        "difficulties",
        "reflection",
        "other",
        "full_log",
    },
    "evaluations": {
        "evaluation_id",
        "person_ref",
        "person_name",
        "source_log",
        "evaluated_at",
        "positive_ai",
        "improvement_ai",
    },
    "human_evaluations": {
        "evaluation_id", "source_log",
        "positive_final", "improvement_final", "submitted_by",
    },
    "reports": {
        "title",
        "reporter_ref",
        "role",
        "reported_at",
    },
    "prompts": {
        "user_ref",
        "role",
        "template",
        "function",
        "review_result",
        "failure_reason",
    },
}

HUMAN_EVALUATION_TABLES = ("human_evaluations", "human_evaluations_backbone")


def human_evaluation_tables(config: TableConfig) -> dict:
    """The original table is now the minister form; the backbone form is separate."""
    return {name: config.tables[name] for name in HUMAN_EVALUATION_TABLES
            if name in config.tables}


def load_table_config(path: str | Path) -> TableConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ValueError(
            f"Feishu table configuration does not exist: {config_path}. "
            "Copy config/tables.example.toml and fill in table IDs."
        )
    with config_path.open("rb") as stream:
        config = TableConfig.model_validate(tomllib.load(stream))
    missing = sorted(REQUIRED_TABLES - set(config.tables))
    if missing:
        raise ValueError(f"Missing Feishu table definitions: {', '.join(missing)}")
    empty_ids = sorted(
        name for name in REQUIRED_TABLES if not config.tables[name].table_id.strip()
    )
    if empty_ids:
        raise ValueError(f"Feishu table IDs are empty: {', '.join(empty_ids)}")
    checked_tables = REQUIRED_TABLES | set(human_evaluation_tables(config))
    for table_name in checked_tables:
        if not config.tables[table_name].table_id.strip():
            raise ValueError(f"Feishu table IDs are empty: {table_name}")
        required_fields = REQUIRED_FIELDS.get(table_name, REQUIRED_FIELDS["human_evaluations"])
        if table_name in HUMAN_EVALUATION_TABLES:
            fields = config.tables[table_name].fields
            if not any(fields.get(key, "").strip()
                       for key in ("basic_name", "backbone_ref", "person_ref")):
                raise ValueError(f"Feishu table {table_name} is missing review subject mapping")
            if "human_evaluations_backbone" in config.tables:
                subject = "basic_name" if table_name.endswith("_backbone") else "backbone_ref"
                required_fields = required_fields | {subject, "evaluated_at",
                    "positive_confirmed", "improvement_confirmed"}
        missing_fields = sorted(
            required_fields - set(config.tables[table_name].fields)
        )
        if missing_fields:
            raise ValueError(
                f"Feishu table {table_name} is missing field mappings: "
                f"{', '.join(missing_fields)}"
            )
        empty_fields = sorted(
            name
            for name in required_fields
            if not config.tables[table_name].fields[name].strip()
        )
        if empty_fields:
            raise ValueError(
                f"Feishu table {table_name} has empty field mappings: "
                f"{', '.join(empty_fields)}"
            )
    return config

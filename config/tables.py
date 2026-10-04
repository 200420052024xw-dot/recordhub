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
        "progress",
        "difficulties",
        "reflection",
        "other",
        "full_log",
    },
    "evaluations": {
        "evaluation_id",
        "person_ref",
        "evaluator_ref",
        "source_log",
        "evaluated_at",
        "positive_ai",
        "improvement_ai",
    },
    "human_evaluations": {
        "evaluation_id", "person_ref", "evaluator_ref",
        "positive_final", "improvement_final", "submitted_by",
    },
    "reports": {
        "title",
        "reporter_ref",
        "role",
        "reported_at",
    },
}


def load_table_config(path: str | Path) -> TableConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ValueError(
            f"Feishu table configuration does not exist: {config_path}. "
            "Create it with the table registry from "
            "tests/test_existing_bitable_layout.py and fill in table IDs."
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
    for table_name in REQUIRED_TABLES:
        required_fields = REQUIRED_FIELDS[table_name]
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

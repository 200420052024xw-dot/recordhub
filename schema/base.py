"""Base types used by RecordHub models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TableDefinition(StrictModel):
    table_id: str
    fields: dict[str, str]


JsonDict = dict[str, Any]

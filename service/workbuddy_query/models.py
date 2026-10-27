"""查询接口的请求契约与业务失败码。

对外只认业务语义（谁 / 哪天到哪天 / 查日志还是阶段工作），不出现表名、列名、
内部字段。业务失败一律走 HTTP 200，原因放 ``code``——对面是模型在判断。
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import ConfigDict, Field

from schema.base import StrictModel

OK = "OK"
NO_PERSON = "NO_PERSON"
DUPLICATE_NAME = "DUPLICATE_NAME"
TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
OUT_OF_SCOPE = "OUT_OF_SCOPE"
RANGE_TOO_LARGE = "RANGE_TOO_LARGE"
RANGE_INVALID = "RANGE_INVALID"
ROLE_NOT_ALLOWED = "ROLE_NOT_ALLOWED"
CONTENT_NOT_SUPPORTED = "CONTENT_NOT_SUPPORTED"
QUERY_FAILED = "QUERY_FAILED"


class QueryError(Exception):
    """业务失败：外层捕获后转成 ``{"code": ...}`` 的 HTTP 200 响应。"""

    def __init__(self, code: str, **extra: object) -> None:
        super().__init__(code)
        self.code = code
        self.extra = extra

    def payload(self) -> dict:
        return {"code": self.code, **self.extra}


class IdentityRequest(StrictModel):
    name: str = Field(min_length=1, max_length=32)


class PreviousEvaluationRequest(StrictModel):
    person_id: str = Field(min_length=1, max_length=64)


class Target(StrictModel):
    type: Literal["self", "person", "group", "department"] = "self"
    name: str | None = Field(default=None, max_length=64)


class RangeSpec(StrictModel):
    """``shortcut`` 与 ``from``/``to`` 二选一，具体校验在 ranges.py。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    shortcut: str | None = None
    from_: date | None = Field(default=None, alias="from")
    to: date | None = None


class QueryRequest(StrictModel):
    person_id: str = Field(min_length=1, max_length=64)
    mode: Literal["view", "review"]
    target: Target | None = None
    range: RangeSpec | None = None
    content: Literal["logs", "stage"] = "logs"
    cursor: str | None = None

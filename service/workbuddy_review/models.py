"""批改接口的请求契约与业务失败码。

批改只负责「收下」：把老师的批改结果暂存到本地，返回「已收到」。真正定稿由后端
夜间与飞书表单合并后统一处理，所以这里不出现「已确认 / 已定稿」。

业务失败一律走 HTTP 200，原因放 ``code``——对面是模型在判断。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from schema.base import StrictModel

OK = "OK"
CLOSED = "CLOSED"
ITEM_NOT_FOUND = "ITEM_NOT_FOUND"
NOT_YOURS = "NOT_YOURS"
EMPTY_EDITED = "EMPTY_EDITED"
REVIEW_FAILED = "REVIEW_FAILED"

# 批改覆盖的产出类型。周报没有审核，不在其中。
KIND_LOG = "log"
KIND_MONTHLY = "monthly"
KIND_STAGE = "stage"
KINDS = (KIND_LOG, KIND_MONTHLY, KIND_STAGE)


class ReviewError(Exception):
    """业务失败：外层捕获后转成 ``{"code": ...}`` 的 HTTP 200 响应。"""

    def __init__(self, code: str, **extra: object) -> None:
        super().__init__(code)
        self.code = code
        self.extra = extra

    def payload(self) -> dict:
        return {"code": self.code, **self.extra}


class PendingRequest(StrictModel):
    person_id: str = Field(min_length=1, max_length=64)
    kind: Literal["log", "monthly", "stage"] = "log"
    cursor: str | None = None


class Section(StrictModel):
    """报告类批改的一段产出。"""

    label: str = Field(min_length=1, max_length=64)
    text: str = Field(default="", max_length=20000)
    url: str = Field(default="", max_length=2000)


class EditedContent(StrictModel):
    """老师改后的内容：日志评价是两栏，报告是分段。"""

    positive: str | None = Field(default=None, max_length=10000)
    improvement: str | None = Field(default=None, max_length=10000)
    sections: list[Section] | None = None

    def normalized(self) -> dict:
        """压成要落盘的形状：只留非空的部分。"""
        result: dict = {}
        positive = (self.positive or "").strip()
        improvement = (self.improvement or "").strip()
        if positive:
            result["positive"] = positive
        if improvement:
            result["improvement"] = improvement
        sections = []
        for section in (self.sections or []):
            data: dict = {"label": section.label}
            if (section.text or "").strip():
                data["text"] = section.text.strip()
            if (section.url or "").strip():
                data["url"] = section.url.strip()
            if len(data) > 1:
                sections.append(data)
        if sections:
            result["sections"] = sections
        return result

    def is_empty(self) -> bool:
        return not self.normalized()


class ReviewSubmitRequest(StrictModel):
    person_id: str = Field(min_length=1, max_length=64)
    item_id: str = Field(min_length=1, max_length=128)
    decision: Literal["accept", "edit"]
    edited: EditedContent | None = None

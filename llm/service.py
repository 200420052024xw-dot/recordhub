"""Structured prompt execution shared by workflows."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from llm.client import DeepSeekClient
from llm.errors import LLMError, LLMValidationError
from tool.errors import DeepSeekApiError, StructuredOutputError

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)

# 一次纠正说明里最多列举几条校验错误，避免提示过长。
REPORTED_ERRORS = 4


def _describe_error(error: dict) -> str:
    """把一条 pydantic 校验错误说成中文短句。"""
    location = ".".join(str(part) for part in error.get("loc") or ())
    kind = str(error.get("type") or "")
    message = str(error.get("msg") or "").strip()
    for prefix in ("Value error, ", "Assertion failed, "):
        if message.startswith(prefix):
            message = message[len(prefix):]
    if kind == "extra_forbidden":
        return f"多出了不允许的字段 {location}"
    if kind == "missing":
        return f"缺少字段 {location}"
    if kind == "string_type":
        return f"字段 {location} 必须是文本"
    if not location:
        return message or "整体结构不符合要求"
    if message:
        return f"字段 {location} 不符合要求：{message}"
    return f"字段 {location} 不符合要求"


def _failure_detail(exc: Exception) -> str:
    """整理校验失败的原因，供下一次请求提示模型。"""
    cause = exc.__cause__ if isinstance(exc.__cause__, ValidationError) else exc
    if isinstance(cause, ValidationError):
        errors = cause.errors()
        described = "；".join(_describe_error(item) for item in errors[:REPORTED_ERRORS])
        if len(errors) > REPORTED_ERRORS:
            described += f"；另有{len(errors) - REPORTED_ERRORS}处同类问题"
        return described
    return str(exc).strip()[:200]


def _repair_note(exc: Exception, output_model: type[BaseModel]) -> str:
    fields = "、".join(output_model.model_fields)
    return (
        f"上一次输出未通过校验：{_failure_detail(exc)}。"
        f"请重新输出一个合法的 JSON 对象，只允许出现这些字段：{fields}。"
        "不要新增未定义的字段，不要输出空字符串或数组；"
        "同一字段内的多条内容写在同一段文字里分点说明，不要拆成多个字段。"
    )


class PromptService:
    def __init__(self, client: DeepSeekClient, *, max_attempts: int = 3) -> None:
        self.client = client
        self.max_attempts = max_attempts

    def execute(
        self, *, prompt_code: str, template: str,
        input_data: BaseModel, output_model: type[T],
        semantic_validator: Callable[[T], None] | None = None,
        retry_feedback: bool = True,
    ) -> T:
        """retry_feedback=False 时每次重试都原样重发，用于判定模板本身是否合格。"""
        base_messages = [
            {"role": "system", "content": (
                f"你正在执行 {prompt_code}。只输出一个合法 JSON 对象，不要输出 "
                "Markdown 代码围栏或额外说明。\n\n"
                f"{template}\n\n"
                "输出必须符合以下 JSON Schema：\n"
                + json.dumps(output_model.model_json_schema(), ensure_ascii=False)
            )},
            {"role": "user", "content": json.dumps(
                input_data.model_dump(mode="json"), ensure_ascii=False)},
        ]
        last_error: Exception | None = None
        raw_output: str | None = None
        repair_note: str | None = None
        for attempt in range(1, self.max_attempts + 1):
            messages = [dict(message) for message in base_messages]
            if repair_note is not None:
                if raw_output:
                    messages.append({"role": "assistant", "content": raw_output})
                messages.append({"role": "user", "content": repair_note})
            parsed: T | None = None
            try:
                parsed = self.client.complete_json(
                    messages, validator=output_model.model_validate)
                if semantic_validator:
                    semantic_validator(parsed)
                if attempt > 1:
                    logger.info("llm_retry_recovered prompt_code=%s attempt=%d/%d",
                                prompt_code, attempt, self.max_attempts)
                return parsed
            except (StructuredOutputError, DeepSeekApiError, ValueError) as exc:
                last_error = exc
                logger.warning("llm_attempt_failed prompt_code=%s attempt=%d/%d error=%s",
                               prompt_code, attempt, self.max_attempts, exc, exc_info=True)
                if attempt == self.max_attempts:
                    break
                raw_output, repair_note = self._retry_hint(
                    exc, parsed, output_model, retry_feedback)
                time.sleep(2 ** (attempt - 1))
        # Only reached when every attempt failed (the success path returns above).
        logger.error("llm_attempts_exhausted prompt_code=%s", prompt_code)
        if isinstance(last_error, StructuredOutputError):
            raise LLMValidationError(str(last_error)) from last_error
        raise LLMError(str(last_error or "Unknown LLM failure")) from last_error

    @staticmethod
    def _retry_hint(exc: Exception, parsed: BaseModel | None,
                    output_model: type[BaseModel],
                    enabled: bool) -> tuple[str | None, str | None]:
        """返回下一次请求要附带的（上一次原始输出, 纠正说明）。"""
        if not enabled or isinstance(exc, DeepSeekApiError):
            # 接口或网络错误与输出格式无关，原样重发才对。
            return None, None
        raw = getattr(exc, "raw", None)
        if not raw and parsed is not None:
            raw = json.dumps(parsed.model_dump(mode="json"), ensure_ascii=False)
        return raw, _repair_note(exc, output_model)

"""Structured prompt execution shared by workflows."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

from llm.client import DeepSeekClient
from llm.errors import LLMError, LLMValidationError
from tool.errors import DeepSeekApiError, StructuredOutputError

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)


class PromptService:
    def __init__(self, client: DeepSeekClient, *, max_attempts: int = 3) -> None:
        self.client = client
        self.max_attempts = max_attempts

    def execute(
        self, *, prompt_code: str, template: str,
        input_data: BaseModel, output_model: type[T],
        semantic_validator: Callable[[T], None] | None = None,
    ) -> T:
        messages = [
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
        for attempt in range(1, self.max_attempts + 1):
            try:
                result = self.client.complete_json(
                    messages, validator=output_model.model_validate)
                if semantic_validator:
                    semantic_validator(result)
                return result
            except (StructuredOutputError, DeepSeekApiError, ValueError) as exc:
                last_error = exc
                logger.warning("llm_attempt_failed prompt_code=%s attempt=%d/%d error=%s",
                               prompt_code, attempt, self.max_attempts, exc, exc_info=True)
                if attempt < self.max_attempts:
                    time.sleep(2 ** (attempt - 1))
        # Only reached when every attempt failed (the success path returns above).
        logger.error("llm_attempts_exhausted prompt_code=%s", prompt_code)
        if isinstance(last_error, StructuredOutputError):
            raise LLMValidationError(str(last_error)) from last_error
        raise LLMError(str(last_error or "Unknown LLM failure")) from last_error

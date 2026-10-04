from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

from config.settings import DeepSeekSettings
from tool.errors import DeepSeekApiError, StructuredOutputError
from tool.http import HttpTransport

T = TypeVar("T")
JsonValidator = Callable[[dict[str, Any]], T]


class DeepSeekClient:
    def __init__(self, settings: DeepSeekSettings, transport: HttpTransport) -> None:
        self.settings = settings
        self.transport = transport

    def complete_text(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str:
        payload = self._complete(messages, temperature, max_tokens, None)
        return self._extract_content(payload)

    def complete_json(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        validator: JsonValidator[T] | None = None,
        temperature: float = 0.1,
        max_tokens: int = 8192,
    ) -> dict[str, Any] | T:
        payload = self._complete(
            messages, temperature, max_tokens, {"type": "json_object"}
        )
        content = self._extract_content(payload)
        if not content.strip():
            raise StructuredOutputError("DeepSeek returned empty structured output")
        try:
            result = json.loads(content)
        except json.JSONDecodeError as exc:
            raise StructuredOutputError("DeepSeek returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise StructuredOutputError("Structured output root must be a JSON object")
        if validator is None:
            return result
        try:
            return validator(result)
        except Exception as exc:
            raise StructuredOutputError(
                f"Structured output validation failed: {exc}"
            ) from exc

    def _complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        temperature: float,
        max_tokens: int,
        response_format: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [dict(message) for message in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if response_format:
            body["response_format"] = dict(response_format)
        response = self.transport.request(
            "POST",
            f"{self.settings.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json_body=body,
        )
        data = response.data if isinstance(response.data, dict) else {}
        if not 200 <= response.status_code < 300 or "error" in data:
            error = data.get("error", {}) if isinstance(data, dict) else {}
            raise DeepSeekApiError(
                str(error.get("message") or "DeepSeek API request failed"),
                status_code=response.status_code,
                code=error.get("code"),
                details=data,
            )
        return data

    @staticmethod
    def _extract_content(payload: Mapping[str, Any]) -> str:
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise DeepSeekApiError("DeepSeek response did not contain message content") from exc
        if not isinstance(content, str):
            raise DeepSeekApiError("DeepSeek message content was not text")
        return content

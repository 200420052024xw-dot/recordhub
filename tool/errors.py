"""Shared infrastructure exceptions."""

from __future__ import annotations

from typing import Any


class RecordHubError(Exception):
    """Base exception for known infrastructure failures."""


class TransportError(RecordHubError):
    """The remote endpoint could not be reached or returned invalid JSON."""


class ProviderApiError(RecordHubError):
    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status_code: int | None = None,
        code: int | str | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.code = code
        self.details = details


class FeishuApiError(ProviderApiError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__("feishu", message, **kwargs)


class DeepSeekApiError(ProviderApiError):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__("deepseek", message, **kwargs)


class StructuredOutputError(RecordHubError):
    """The model response was invalid or did not match the requested contract."""

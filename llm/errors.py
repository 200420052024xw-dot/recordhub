"""Errors raised by shared LLM execution."""


class LLMError(Exception):
    pass


class LLMValidationError(LLMError):
    pass

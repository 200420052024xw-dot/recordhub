from __future__ import annotations


class WorkflowError(Exception):
    """Base class for known Workflow1 failures."""


class FeishuReadError(WorkflowError):
    pass


class LLMError(WorkflowError):
    pass


class LLMValidationError(LLMError):
    pass


class WorkflowStateError(WorkflowError):
    pass




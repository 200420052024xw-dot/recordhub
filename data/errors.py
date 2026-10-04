"""Failures raised by shared repositories and state storage."""


class DataError(Exception):
    """Base class for known data-layer failures."""


class FeishuReadError(DataError):
    pass


class WorkflowStateError(DataError):
    pass

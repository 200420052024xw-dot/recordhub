"""Serializable diagnostic events, captured before crossing thread boundaries."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import threading
import time
import traceback
from collections import deque
from datetime import UTC, datetime
from uuid import uuid4

from tool.diagnostics import DiagnosticFormatter, CONTEXT_LABELS, EXCEPTION_EXPLANATIONS, _context, redact

INSTANCE = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
_recent = deque()
_recent_bytes = 0
_recent_lock = threading.Lock()
_release = "unknown"
_secrets = ()


def safe_text(value: str) -> str:
    # Mask named credentials first: a short configured secret must not alter a
    # label such as api_key before the credential regex has seen its value.
    value = redact(value)
    for secret in _secrets:
        if len(secret) >= 8:
            value = value.replace(secret, "[REDACTED]")
        else:
            value = re.sub(r"(?<![\w])" + re.escape(secret) + r"(?![\w]|\s*[:=])", "[REDACTED]", value)
    value = re.sub(r"\bou_[A-Za-z0-9_-]+", "[OPEN_ID]", value)
    value = re.sub(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)", "[PHONE]", value)
    value = re.sub(r"(?i)((?:mobile|phone|open_id|recipient|user_id|to)=)[^\s,]+", r"\1[REDACTED]", value)
    return value


def sanitize(value):
    """Keep business content out of technical diagnostics, recursively."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            name = str(key).lower()
            if name == "token" or name.endswith("_token") or any(part in name for part in (
                    "secret", "authorization", "password", "api_key", "access_key", "ticket")):
                result[key] = "[REDACTED]"
            elif name in {"mobile", "phone", "open_id", "recipient", "to", "user_id"}:
                result[key] = "[REDACTED]"
            elif name in {"messages", "raw", "raw_output", "input", "output", "progress", "difficulties", "reflection", "full_log", "content"}:
                result[key] = "[BUSINESS_CONTENT_OMITTED]"
            else:
                result[key] = sanitize(item)
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return safe_text(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return safe_text(str(value))


def install_record_factory(release="unknown", secrets=()):
    global _release, _secrets
    _release = release
    _secrets = tuple(sorted({s for s in secrets if s}, key=len, reverse=True))
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_recordhub", False):
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        record.diagnostic_snapshot = dict(_context.get())
        record._recordhub_event_id = uuid4().hex
        return record

    factory._recordhub = True
    logging.setLogRecordFactory(factory)


def event_from_record(record):
    if hasattr(record, "diagnostic_event"):
        return record.diagnostic_event
    context = dict(getattr(record, "diagnostic_snapshot", _context.get()))
    if record.exc_info and record.exc_info[1]:
        context.update(getattr(record.exc_info[1], "_recordhub_context", {}))
    record.diagnostic_snapshot = context
    message = safe_text(record.getMessage())
    # Legacy call sites retain their text, but common key=value fields are queryable.
    fields = dict(re.findall(r"\b([a-z][a-z0-9_]*)=([^\s]+)", message))
    fields.update(getattr(record, "fields", {}))
    context.update(getattr(record, "business_context", {}))
    event = {
        "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
        "level": record.levelname, "logger": record.name,
        "event_name": getattr(record, "event_name", message.split(maxsplit=1)[0] if message else "log"),
        "event_id": getattr(record, "event_id", getattr(record, "_recordhub_event_id", uuid4().hex)),
        "execution_id": context.get("execution_id", "-"),
        "release": _release, "instance": INSTANCE,
        "context": context, "fields": fields, "message": message,
        "explanation": DiagnosticFormatter._explanation(record),
        "code": {"file": record.pathname, "line": record.lineno, "function": record.funcName},
        "alert": getattr(record, "alert", None),
    }
    if record.exc_info and record.exc_info[1]:
        exc = record.exc_info[1]
        if getattr(exc, "_recordhub_stage", None):
            event["context"]["stage"] = exc._recordhub_stage
        chain = []
        trace_parts = []
        seen = set()
        while exc is not None and id(exc) not in seen:
            seen.add(id(exc))
            frames = traceback.extract_tb(exc.__traceback__)
            message = str(exc) or "(no message)"
            if hasattr(exc, "errors"):
                message = json.dumps(exc.errors(include_input=False, include_context=False), ensure_ascii=False, default=str)
            chain.append({"type": type(exc).__name__, "message": message,
                          "code": getattr(exc, "code", None), "status_code": getattr(exc, "status_code", None),
                          "request_id": getattr(exc, "request_id", None), "path": getattr(exc, "path", None),
                          "location": f"{frames[-1].filename}:{frames[-1].name}:{frames[-1].lineno}" if frames else ""})
            trace_parts.append("Traceback (most recent call last):\n" + "".join(traceback.format_list(frames))
                               + f"{type(exc).__name__}: {message}\n")
            exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
        event["exception"] = {"chain": chain, "traceback": "\nCaused by / exception context:\n".join(reversed(trace_parts))}
    event = sanitize(event)
    record.diagnostic_event = event
    return event


def purge_recent(retention_days=3, now=None):
    global _recent_bytes
    cutoff = (time.time() if now is None else now) - retention_days * 86400
    with _recent_lock:
        kept = [(e, size) for e, size in _recent if datetime.fromisoformat(e["timestamp"]).timestamp() >= cutoff]
        _recent.clear()
        _recent.extend(kept)
        _recent_bytes = sum(size for _, size in kept)


class EventFilter(logging.Filter):
    def filter(self, record):
        global _recent_bytes
        event = event_from_record(record)
        if not getattr(record, "_remembered", False):
            # A byte budget prevents a handful of giant stack traces retaining GBs.
            encoded = json.dumps(event, ensure_ascii=False)
            if len(encoded.encode("utf-8")) <= 24 * 1024:
                with _recent_lock:
                    size = len(encoded.encode("utf-8"))
                    _recent.append((event, size))
                    _recent_bytes += size
                    while _recent_bytes > 2 * 1024 * 1024 or len(_recent) > 2000:
                        _, removed_size = _recent.popleft()
                        _recent_bytes -= removed_size
            record._remembered = True
        return True


class JSONFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(event_from_record(record), ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record):
        e = event_from_record(record)
        return f"{e['timestamp']} {e['level']} {e['event_name']} {e['explanation']} execution={e['execution_id']} event={e['event_id']} {e['message']}"


def recent_evidence(event, retention_days=3):
    cutoff = time.time() - retention_days * 86400
    with _recent_lock:
        items = [e for e, _ in _recent if datetime.fromisoformat(e["timestamp"]).timestamp() >= cutoff
                 and event["execution_id"] != "-" and e["execution_id"] == event["execution_id"]]
    items = [e for e in items if e["event_id"] != event["event_id"]][-39:] + [event]
    # Preserve the full raw event separately; model input is explicitly bounded.
    selected = []
    for item in reversed(items):
        candidate = dict(item)
        if "exception" in candidate:
            candidate["exception"] = dict(candidate["exception"])
            candidate["exception"]["traceback"] = candidate["exception"]["traceback"][-12000:]
        while len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) > 23 * 1024:
            candidate["message"] = candidate["message"][:max(0, len(candidate["message"]) // 2)]
            candidate["fields"] = {}
            if "exception" in candidate:
                candidate["exception"] = {"traceback": candidate["exception"].get("traceback", "")[-4000:]}
            candidate["truncated"] = True
            if not candidate["message"]:
                break
        if len(json.dumps(selected + [candidate], ensure_ascii=False).encode("utf-8")) > 24 * 1024:
            break
        selected.append(candidate)
    return list(reversed(selected))


def incident_scope(event):
    c, f = event["context"], event["fields"]
    return {"workflow": c.get("workflow", event["logger"]),
            "target_date": c.get("target_date", f.get("date", "")),
            "run_id": c.get("run_id", f.get("run_id", "")),
            "stage": c.get("stage", f.get("stage", c.get("operation", event["event_name"]))),
            "object": c.get("log_id", c.get("notification_key", f.get("log_id", f.get("key", ""))))}


def fingerprint(event):
    chain = event.get("exception", {}).get("chain", [])
    reason = chain[-1] if chain else {}
    # Unclassified messages need a stable discriminator so unrelated SDK errors differ.
    message = re.sub(r"\b\w+=[^\s]+", "", event["message"])
    path = re.sub(r"/(?:rec|tbl|app|doc)[A-Za-z0-9_-]+", "/:id", reason.get("path") or event["fields"].get("path", ""))
    code = reason.get("code") or event["fields"].get("code")
    detail = reason.get("message", "")
    detail = re.sub(r"\b(?:[a-fA-F0-9]{16,}|\d{4}-\d{2}-\d{2})\b", ":id", detail)
    detail = re.sub(r"\b\w+=[^\s]+", "", detail)
    identity = [incident_scope(event), event["event_name"], reason.get("type"),
                code, reason.get("status_code") or event["fields"].get("status_code"), reason.get("location"), path,
                message if not chain else (detail if not code else "")]
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def raw_error(event):
    """Original error text, never a model-generated fallback summary."""
    text = f"{event['timestamp']} {event['level']} {event['logger']}: {event['message']}"
    if event.get("exception"):
        text += "\n" + event["exception"]["traceback"]
        text += "\n异常属性=" + json.dumps(event["exception"].get("chain", []), ensure_ascii=False)
    if event["explanation"]:
        text += "\n中文说明=" + event["explanation"]
    text += "\n业务上下文=" + json.dumps({CONTEXT_LABELS.get(k, k): v for k, v in event["context"].items()}, ensure_ascii=False)
    text += f"\n事件编号={event['event_id']} 执行编号={event['execution_id']}"
    for exc in event.get("exception", {}).get("chain", []):
        if exc["type"] in EXCEPTION_EXPLANATIONS:
            text += "\n错误类型说明=" + EXCEPTION_EXPLANATIONS[exc["type"]]
    return safe_text(text)

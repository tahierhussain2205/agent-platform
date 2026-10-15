"""Structured JSON logging with request, tenant and trace context, and a redaction backstop.

Code must never log secrets (invariant 5). The redaction filter is a second line of defense for
values that slip into messages or exception text: bearer tokens, Slack tokens, API keys, private
key blocks and passwords embedded in URLs.
"""

import json
import logging
import re
import sys
import traceback
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace

from app.context import org_id_var, request_id_var

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{8,}"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"\bxapp-[A-Za-z0-9-]{8,}"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})"), "[REDACTED_GITHUB_TOKEN]"),
    (
        re.compile(r"(?i)\b(postgres(?:ql)?|redis|rediss|mysql|amqp)://([^:/@\s]+):([^@\s]+)@"),
        r"\1://\2:[REDACTED]@",
    ),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (re.compile(r"(?i)\b(password|passwd|secret|api_key|apikey|token)=([^\s&]+)"), r"\1=[REDACTED]"),
]

# Attributes every LogRecord has; anything else passed via `extra=` is emitted as a field.
_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
            "service": self.service,
        }
        if (request_id := request_id_var.get()) is not None:
            entry["request_id"] = request_id
        if (org_id := org_id_var.get()) is not None:
            entry["org_id"] = str(org_id)
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            entry["trace_id"] = format(span.trace_id, "032x")
            entry["span_id"] = format(span.span_id, "016x")
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = redact(value) if isinstance(value, str) else value
        if record.exc_info:
            entry["exc"] = redact("".join(traceback.format_exception(*record.exc_info)))
        return json.dumps(entry, default=str)


def configure_logging(level: str, service: str, json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if json_output:
        handler.setFormatter(JsonFormatter(service))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Our middleware writes one access line per request; uvicorn's would duplicate it.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True

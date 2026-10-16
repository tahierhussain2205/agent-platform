"""One observability interface over log backends (spec 7.2).

The agent sees three operations whatever the backend: search events by service, window, severity
and text; list error groups; fetch a trace. Backends push filtering to their own query language
and return events, which are grouped here by **error signature**: the exception type, the message
with its variable parts replaced, and the top frame of the stack. Groups carry counts, first and
last seen and a few samples, so the model reads aggregates, not raw logs.

Which logs belong to a service is the catalog's job (``services.log_source``): the agent names a
service, never a project or a query.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

SEVERITIES = ("DEFAULT", "DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY")
MAX_WINDOW = timedelta(days=7)
DEFAULT_WINDOW = timedelta(hours=24)
MAX_SAMPLES = 3

FRAME = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')
EXCEPTION = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Fault))\b:?\s*(.*)$")
VARIABLE = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<str>"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "<email>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?"), "<n>"),
]


class ObservabilityError(Exception):
    """A request the interface cannot serve (no log source, a window too wide); the message says why."""


@dataclass(frozen=True)
class LogSource:
    """Where a service's logs are (from the catalog)."""

    service: str
    provider: str
    project: str
    resource_type: str | None = None
    label: str = "service_name"  # the resource label carrying the service's name
    value: str = ""  # its value (the service's name by default)

    def __post_init__(self) -> None:
        if not self.value:
            object.__setattr__(self, "value", self.service)

    @classmethod
    def from_catalog(cls, service: str, data: dict[str, Any]) -> "LogSource":
        provider, project = str(data.get("provider") or ""), str(data.get("project") or "")
        if not provider or not project:
            raise ObservabilityError(
                f"The catalog has no log source for {service}: an admin sets it on the Catalog page."
            )
        return cls(service, provider, project, data.get("resource_type") or None,
                   str(data.get("label") or "service_name"), str(data.get("value") or service))  # fmt: skip


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    @classmethod
    def parse(cls, start: Any, end: Any, now: datetime | None = None) -> "Window":
        """From ISO times (the brief's window); the last 24 hours when none is given; at most 7 days,
        never in the future."""
        now = now or datetime.now(UTC)

        def when(value: Any) -> datetime | None:
            if not value:
                return None
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ObservabilityError(f"{value} is not a time like 2026-10-16T09:00:00Z") from exc
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

        stop = min(when(end) or now, now)
        begin = when(start) or stop - DEFAULT_WINDOW
        if begin >= stop:
            raise ObservabilityError("The window's start must be before its end.")
        if stop - begin > MAX_WINDOW:
            raise ObservabilityError("A window covers at most 7 days; narrow it to the incident.")
        return cls(begin, stop)


@dataclass
class Event:
    timestamp: str
    severity: str
    message: str
    exception: str | None = None
    trace_id: str | None = None
    labels: dict[str, Any] = field(default_factory=dict)


@dataclass
class EventGroup:
    signature: str
    exception_type: str | None
    top_frame: str | None
    severity: str
    count: int
    first_seen: str
    last_seen: str
    samples: list[Event] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signature": self.signature, "exception_type": self.exception_type, "top_frame": self.top_frame,
            "severity": self.severity, "count": self.count, "first_seen": self.first_seen,
            "last_seen": self.last_seen, "samples": [s.__dict__ for s in self.samples],
        }  # fmt: skip


@dataclass
class SearchResult:
    groups: list[EventGroup]
    total: int
    complete: bool  # False when the backend had more events than were read
    query: str
    link: str | None = None


@dataclass
class ErrorGroup:
    signature: str
    count: int
    first_seen: str
    last_seen: str
    message: str
    link: str | None = None


@dataclass
class Span:
    name: str
    service: str | None
    start: str
    duration_ms: float
    status: str | None
    parent: str | None = None


def normalize(text: str) -> str:
    out = text.strip()
    for pattern, replacement in VARIABLE:
        out = pattern.sub(replacement, out)
    return re.sub(r"\s+", " ", out)[:300]


def parse_exception(text: str | None) -> tuple[str | None, str | None, str | None]:
    """(exception type, its message, the innermost frame as path:line in function) of a traceback or
    an error line. Paths lose a leading app root (``/app/``, ``/srv/``, ``/workspace/``)."""
    if not text:
        return None, None, None
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    frames = FRAME.findall(text)
    top = None
    if frames:
        path, line, func = frames[-1]
        path = re.sub(r"^/(?:app|srv|workspace|code)/", "", path)
        top = f"{path}:{line} in {func}"
    for line in reversed(lines):
        m = EXCEPTION.match(line)
        if m:
            return m.group(1), m.group(2), top
    return None, lines[-1] if lines else None, top


def signature(message: str, exception: str | None = None) -> tuple[str, str | None, str | None]:
    """(signature, exception type, top frame): what makes two events the same problem."""
    kind, detail, top = parse_exception(exception)
    if kind is None and exception is None:
        kind, detail, top = parse_exception(message)
    text = normalize(f"{kind}: {detail}" if kind else (detail or message))
    return (f"{text} @ {top}" if top else text), kind, top


def group(events: list[Event]) -> list[EventGroup]:
    """Events by signature, most frequent first (ties: most recent first)."""
    groups: dict[str, EventGroup] = {}
    for e in events:
        sig, kind, top = signature(e.message, e.exception)
        g = groups.get(sig)
        if g is None:
            g = groups[sig] = EventGroup(sig, kind, top, e.severity, 0, e.timestamp, e.timestamp)
        g.count += 1
        g.first_seen, g.last_seen = min(g.first_seen, e.timestamp), max(g.last_seen, e.timestamp)
        if SEVERITIES.index(e.severity) > SEVERITIES.index(g.severity):
            g.severity = e.severity
        if len(g.samples) < MAX_SAMPLES:
            g.samples.append(e)
    return sorted(groups.values(), key=lambda g: (g.count, g.last_seen), reverse=True)


class LogBackend(Protocol):
    async def search(
        self, source: LogSource, window: Window, severity: str, text: str, limit: int
    ) -> SearchResult: ...

    async def error_groups(self, source: LogSource, window: Window) -> list[ErrorGroup]: ...

    async def trace(self, source: LogSource, trace_id: str) -> list[Span]: ...

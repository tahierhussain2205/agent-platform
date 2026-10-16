"""Google Cloud backend of the observability interface: Cloud Logging (``entries:list`` with a
filter in the Logging query language), Error Reporting (``groupStats``) and Cloud Trace.

Filtering happens at Google: the service's resource and label, the severity, the window and the
text are all in the filter; at most ``MAX_EVENTS`` events are read (newest first) and grouped.
"""

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.observability import (
    SEVERITIES,
    ErrorGroup,
    Event,
    LogSource,
    ObservabilityError,
    SearchResult,
    Span,
    Window,
    group,
    signature,
)
from app.tools.http import request

MAX_EVENTS = 1000
PAGE = 500
PERIODS = [(1, "PERIOD_1_HOUR"), (6, "PERIOD_6_HOURS"), (24, "PERIOD_1_DAY"), (168, "PERIOD_1_WEEK"),
           (720, "PERIOD_30_DAYS")]  # fmt: skip


class GcpError(ObservabilityError):
    pass


def quoted(value: str) -> str:
    """A string literal in the Logging query language."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def iso(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_filter(source: LogSource, window: Window, severity: str, text: str) -> str:
    clauses = []
    if source.resource_type:
        clauses.append(f"resource.type={quoted(source.resource_type)}")
    clauses.append(f"resource.labels.{source.label}={quoted(source.value)}")
    if severity and severity != "DEFAULT":
        clauses.append(f"severity>={severity}")
    clauses += [f"timestamp>={quoted(iso(window.start))}", f"timestamp<={quoted(iso(window.end))}"]
    if text.strip():
        t = quoted(text.strip()[:200])
        clauses.append(f"(textPayload:{t} OR jsonPayload.message:{t} OR jsonPayload.exception:{t})")
    return " AND ".join(clauses)


def explorer_link(source: LogSource, query: str, window: Window) -> str:
    return (
        f"https://console.cloud.google.com/logs/query;query={quote(query, safe='')};"
        f"timeRange={quote(iso(window.start), safe='')}%2F{quote(iso(window.end), safe='')}"
        f"?project={quote(source.project)}"
    )


def to_event(entry: dict[str, Any]) -> Event:
    payload = entry.get("jsonPayload") or {}
    message = str(payload.get("message") or entry.get("textPayload") or "")
    labels = {
        k: v
        for k, v in payload.items()
        if k not in ("message", "exception") and isinstance(v, str | int | float)
    }
    trace = str(entry.get("trace") or "").rsplit("/", 1)[-1] or None
    exception = str(payload["exception"])[:4000] if payload.get("exception") else None
    severity = str(entry.get("severity") or "DEFAULT")
    return Event(str(entry.get("timestamp") or ""), severity, message[:500], exception, trace, labels)


class GoogleCloudLogging:
    def __init__(
        self, http: httpx.AsyncClient, token: str, logging_url: str, errors_url: str, trace_url: str
    ) -> None:
        self.http = http
        self.headers = {"authorization": f"Bearer {token}"}
        self.logging_url, self.errors_url, self.trace_url = logging_url, errors_url, trace_url

    async def _call(self, method: str, url: str, **kw: Any) -> Any:
        response = await request(self.http, method, url, headers=self.headers, **kw)
        if response.status_code >= 400:
            try:
                message = str(response.json().get("error", {}).get("message") or response.status_code)
            except ValueError:
                message = f"HTTP {response.status_code}"
            if response.status_code in (401, 403):
                raise GcpError(f"Google Cloud refused access: {message}")
            raise GcpError(f"Google Cloud answered {response.status_code}: {message}")
        return response.json()

    async def search(
        self, source: LogSource, window: Window, severity: str, text: str, limit: int
    ) -> SearchResult:
        if severity not in SEVERITIES:
            raise ObservabilityError(f"severity is one of {', '.join(SEVERITIES)}")
        query = build_filter(source, window, severity, text)
        events: list[Event] = []
        token: str | None = None
        complete = True
        while len(events) < MAX_EVENTS:
            body: dict[str, Any] = {"resourceNames": [f"projects/{source.project}"], "filter": query,
                                    "orderBy": "timestamp desc", "pageSize": PAGE}  # fmt: skip
            if token:
                body["pageToken"] = token
            page = await self._call("POST", f"{self.logging_url}/v2/entries:list", json=body)
            events += [to_event(e) for e in page.get("entries") or []]
            token = page.get("nextPageToken")
            if not token:
                break
        else:
            complete = token is None
        groups = group(events[:MAX_EVENTS])
        for g in groups:
            g.samples = g.samples[: max(1, min(limit, 3))]
        return SearchResult(groups, len(events), complete and len(events) <= MAX_EVENTS, query,
                            explorer_link(source, query, window))  # fmt: skip

    async def error_groups(self, source: LogSource, window: Window) -> list[ErrorGroup]:
        hours = (datetime.now(UTC) - window.start).total_seconds() / 3600
        period = next((p for h, p in PERIODS if hours <= h), PERIODS[-1][1])
        params = {"timeRange.period": period, "serviceFilter.service": source.value, "pageSize": "100"}
        body = await self._call(
            "GET", f"{self.errors_url}/v1beta1/projects/{quote(source.project)}/groupStats", params=params
        )
        out = []
        for g in body.get("errorGroupStats") or []:
            first, last = str(g.get("firstSeenTime") or ""), str(g.get("lastSeenTime") or "")
            if (last and last < iso(window.start)) or (first and first > iso(window.end)):
                continue
            message = str((g.get("representative") or {}).get("message") or "")
            sig = signature(message.splitlines()[0] if message else "", message)[0]
            group_id = (g.get("group") or {}).get("groupId")
            link = f"https://console.cloud.google.com/errors/detail/{group_id}?project={source.project}"
            out.append(ErrorGroup(sig, int(g.get("count") or 0), first, last, message[-500:], link))
        return out

    async def trace(self, source: LogSource, trace_id: str) -> list[Span]:
        if not trace_id.isalnum() or len(trace_id) > 64:
            raise ObservabilityError("A trace id is up to 64 letters and digits.")
        body = await self._call(
            "GET", f"{self.trace_url}/v1/projects/{quote(source.project)}/traces/{trace_id}"
        )
        spans = []
        for s in body.get("spans") or []:
            start = datetime.fromisoformat(str(s["startTime"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(s["endTime"]).replace("Z", "+00:00"))
            labels = s.get("labels") or {}
            spans.append(
                Span(
                    str(s.get("name")),
                    labels.get("service"),
                    iso(start),
                    round((end - start).total_seconds() * 1000, 1),
                    labels.get("/http/status_code") or labels.get("error"),
                    s.get("parentSpanId"),
                )
            )
        return spans

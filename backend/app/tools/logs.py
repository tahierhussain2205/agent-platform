"""Logs, error groups and traces through the observability interface (spec 7.2), here for Google
Cloud Logging (integration ``google_cloud_logging``, family ``observability``).

The model names a catalog service and a window; the service's log source (project, resource,
label) comes from the catalog, so the agent reads only what admins pointed it at. Results are
grouped by error signature with counts and samples, capped, redacted, and cited as restricted
sources: posting them to a channel needs every member to be an active member of the org.
"""

from typing import Any

from app.db.tenant import tenant_tx
from app.observability import LogSource, ObservabilityError, Window
from app.observability.gcp import GoogleCloudLogging, build_filter, explorer_link
from app.policy import Member
from app.policy.redaction import redact
from app.tools.base import Operation, Source, Target, ToolContext, ToolResult, cap
from app.vault_service import Credential

WINDOW = {
    "start": {"type": "string", "description": "ISO time, e.g. 2026-10-16T06:00:00Z (default: 24 hours ago)"},
    "end": {"type": "string", "description": "ISO time (default: now)"},
}
OPERATIONS = {
    "search_events": Operation(
        "search_events",
        "Search a service's logs in a time window, grouped by error signature with counts, first and "
        "last seen and samples. Give the service's catalog name; filter by minimum severity and text.",
        {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                **WINDOW,
                "severity": {"type": "string", "enum": ["DEFAULT", "INFO", "WARNING", "ERROR", "CRITICAL"]},
                "text": {"type": "string", "maxLength": 200},
                "samples": {"type": "integer", "minimum": 1, "maximum": 3},
            },
            "required": ["service"],
        },
    ),
    "list_error_groups": Operation(
        "list_error_groups",
        "List a service's error groups (Error Reporting) seen in a time window, most frequent first.",
        {"type": "object", "properties": {"service": {"type": "string"}, **WINDOW}, "required": ["service"]},
    ),
    "get_trace": Operation(
        "get_trace",
        "Fetch one trace of a service (spans with durations and statuses) by its trace id from a log sample.",
        {
            "type": "object",
            "properties": {"service": {"type": "string"}, "trace_id": {"type": "string"}},
            "required": ["service", "trace_id"],
        },
    ),
}


class LogsAdapter:
    key = "google_cloud_logging"
    family = "observability"
    operations = OPERATIONS

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    async def resolve_target(self, ctx: ToolContext, operation: str, args: dict[str, Any]) -> Target | None:
        return None

    async def _source(self, ctx: ToolContext, service: str) -> LogSource:
        async with tenant_tx(ctx.pool, ctx.org_id) as conn:
            row = await (
                await conn.execute(
                    "select name, log_source from app.services where status = 'active' "
                    "and lower(btrim(name)) = lower(btrim(%s))",
                    (service,),
                )
            ).fetchone()
        if row is None:
            raise ObservabilityError(f"No confirmed service named {service!r} is in the catalog.")
        source = LogSource.from_catalog(row[0], row[1] or {})
        if source.provider != self.key:
            raise ObservabilityError(f"{row[0]}'s logs are not in Google Cloud Logging.")
        return source

    def _backend(self, ctx: ToolContext, credential: Credential) -> GoogleCloudLogging:
        s = self.settings
        return GoogleCloudLogging(
            ctx.http, credential.token, s.gcp_logging_url, s.gcp_error_reporting_url, s.gcp_trace_url
        )

    def _cite(self, ctx: ToolContext, source: LogSource, ref: str, label: str, link: str | None) -> str:
        return ctx.refs.add(
            Source(
                ref=f"logs:{source.project}:{ref}",
                channel_external_id="",
                channel_name=f"{source.service} logs",
                visibility="restricted",
                ts="",
                label=label[:80],
                permalink=link,
                kind="logs",
                audience=f"{self.key}:{source.project}",
            )
        )

    async def audience_ok(
        self, ctx: ToolContext, credential: Credential, source: Source, members: list[Member]
    ) -> bool:
        """Logs are the org's: everyone at the destination must be an active member of it."""
        ids = [str(m.membership_id) for m in members]
        if not ids:
            return True
        async with tenant_tx(ctx.pool, ctx.org_id) as conn:
            row = await (
                await conn.execute(
                    "select count(*) from app.org_memberships "
                    "where id = any(%s::uuid[]) and status = 'active'",
                    (ids,),
                )
            ).fetchone()
        return bool(row and row[0] == len(set(ids)))

    async def execute(
        self, ctx: ToolContext, operation: str, args: dict[str, Any], credential: Any = None
    ) -> ToolResult:
        if not isinstance(credential, Credential):
            return ToolResult("Google Cloud Logging is not connected.", is_error=True)
        try:
            source = await self._source(ctx, str(args.get("service") or "").strip())
            backend = self._backend(ctx, credential)
            if operation == "search_events":
                return await self._search(ctx, backend, source, args)
            if operation == "list_error_groups":
                return await self._errors(ctx, backend, source, args)
            if operation == "get_trace":
                return await self._trace(ctx, backend, source, str(args.get("trace_id") or "").strip())
        except ObservabilityError as exc:
            return ToolResult(str(exc), is_error=True)
        return ToolResult(f"Unknown operation {operation}.", is_error=True)

    async def _search(
        self, ctx: ToolContext, backend: GoogleCloudLogging, source: LogSource, args: dict[str, Any]
    ) -> ToolResult:
        window = Window.parse(args.get("start"), args.get("end"))
        severity = str(args.get("severity") or "DEFAULT").upper()
        result = await backend.search(
            source, window, severity, str(args.get("text") or ""), int(args.get("samples") or 2)
        )
        head = (
            f"{source.service} logs {window.start:%Y-%m-%d %H:%M}Z to {window.end:%Y-%m-%d %H:%M}Z"
            f"{' at ' + severity + ' or above' if severity != 'DEFAULT' else ''}: {result.total} events"
            f"{' (more exist; narrow the window or filter)' if not result.complete else ''}, "
            f"{len(result.groups)} signatures."
        )
        lines = [head]
        for g in result.groups[:15]:
            query = build_filter(source, window, severity, g.samples[0].message[:60] if g.samples else "")
            short = self._cite(ctx, source, g.signature, g.signature, explorer_link(source, query, window))
            samples = "; ".join(
                f"{s.timestamp[:19]}Z {s.message[:120]}"
                + (" " + " ".join(f"{k}={v}" for k, v in list(s.labels.items())[:4]) if s.labels else "")
                + (f" trace {s.trace_id}" if s.trace_id else "")
                for s in g.samples
            )
            lines.append(
                f"[{short}] {g.severity} x{g.count} ({g.first_seen[:16]}Z to {g.last_seen[:16]}Z): "
                f"{g.signature}. Samples: {samples}"
            )
        if not result.groups:
            lines.append("No events match.")
        data = {"service": source.service, "window": [window.start.isoformat(), window.end.isoformat()],
                "total": result.total, "groups": [g.to_dict() for g in result.groups[:15]]}  # fmt: skip
        return ToolResult(cap(redact("\n".join(lines))[0]), data=data)

    async def _errors(
        self, ctx: ToolContext, backend: GoogleCloudLogging, source: LogSource, args: dict[str, Any]
    ) -> ToolResult:
        window = Window.parse(args.get("start"), args.get("end"))
        groups = await backend.error_groups(source, window)
        lines = [
            f"Error groups of {source.service} seen "
            f"{window.start:%Y-%m-%d %H:%M}Z to {window.end:%Y-%m-%d %H:%M}Z:"
        ]
        for g in groups[:15]:
            short = self._cite(ctx, source, f"group:{g.signature}", g.signature, g.link)
            lines.append(f"[{short}] x{g.count} ({g.first_seen[:16]}Z to {g.last_seen[:16]}Z): {g.signature}")
        if not groups:
            lines.append("No error groups in this window.")
        data = {"service": source.service, "groups": [g.__dict__ for g in groups[:15]]}
        return ToolResult(cap(redact("\n".join(lines))[0]), data=data)

    async def _trace(
        self, ctx: ToolContext, backend: GoogleCloudLogging, source: LogSource, trace_id: str
    ) -> ToolResult:
        spans = await backend.trace(source, trace_id)
        link = f"https://console.cloud.google.com/traces/list?project={source.project}&tid={trace_id}"
        short = self._cite(ctx, source, f"trace:{trace_id}", f"trace {trace_id[:12]}", link)
        lines = [f"[{short}] Trace {trace_id} of {source.service} ({len(spans)} spans):"]
        lines += [
            f"- {s.name} ({s.service or 'unknown'}): {s.duration_ms} ms"
            + (f", status {s.status}" if s.status else "")
            for s in spans[:50]
        ]
        return ToolResult(cap(redact("\n".join(lines))[0]), data={"spans": [s.__dict__ for s in spans[:50]]})

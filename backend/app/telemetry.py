"""OpenTelemetry tracing: one trace per request or task, tenant id on every span.

Binding a unit of work sets ``tenant.id`` on the current span and in ``org_id_var``; the
``TenantSpanProcessor`` stamps it on every span started afterwards (database calls, outbound
requests, child work). Query strings are stripped from server span attributes because they can
carry credentials (OAuth codes, signed URLs).
"""

from collections.abc import Sequence
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter

from app.context import org_id_var

TENANT_ATTRIBUTE = "tenant.id"
_configured = False


class TenantSpanProcessor(SpanProcessor):
    def on_start(self, span: Span, parent_context: otel_context.Context | None = None) -> None:
        org_id = org_id_var.get()
        if org_id is not None:
            span.set_attribute(TENANT_ATTRIBUTE, str(org_id))

    def on_end(self, span: ReadableSpan) -> None:
        pass


def configure_tracing(
    service_name: str,
    otlp_endpoint: str | None,
    environment: str,
    extra_exporters: Sequence[SpanExporter] = (),
) -> None:
    """Install the global tracer provider once per process. Without an endpoint (and no extra
    exporters) spans are created for context propagation but exported nowhere."""
    global _configured
    if _configured:
        return
    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name, "deployment.environment": environment})
    )
    provider.add_span_processor(TenantSpanProcessor())
    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{otlp_endpoint}/v1/traces"))
        )
    for exporter in extra_exporters:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

    # Statement text only (never parameters); no SQL commenter, which would defeat statement caching.
    PsycopgInstrumentor().instrument(enable_commenter=False, skip_dep_check=True)
    _configured = True


def shutdown_tracing() -> None:
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.force_flush(timeout_millis=5000)


_URL_ATTRIBUTES = ("http.url", "http.target", "url.full", "url.query")


def strip_query_strings(span: trace.Span, scope: dict[str, Any]) -> None:
    """ASGI server request hook: drop query strings from URL attributes."""
    if not span.is_recording():
        return
    path = scope.get("path", "")
    span.set_attribute("http.target", path)
    span.set_attribute("url.path", path)
    for attribute in ("http.url", "url.full"):
        span.set_attribute(attribute, path)
    span.set_attribute("url.query", "")


def mark_tenant(org_id: object) -> None:
    """Called when a unit of work binds: tag the current span with the tenant."""
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute(TENANT_ATTRIBUTE, str(org_id))

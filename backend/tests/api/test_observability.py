"""Structured logs, request ids, redaction and tracing (tenant id on every span)."""

import json
import logging
import uuid

import httpx
import pytest

from app.context import org_id_var, request_id_var
from app.logging import JsonFormatter, redact
from app.telemetry import TENANT_ATTRIBUTE
from tests.conftest import SPANS
from tests.world import World

# ------------------------------------------------------------------ redaction


@pytest.mark.parametrize(
    ("raw", "leaked"),
    [
        ("Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.payload.sig", "eyJhbGciOiJSUzI1NiJ9"),
        ("token xoxb-1234567890-abcdefghijkl leaked", "xoxb-1234567890"),
        ("app token xapp-1-A0123-4567-abcdef", "xapp-1-A0123"),
        ("key sk-ant-api03-abcdefghijklmnop", "sk-ant-api03"),
        ("gh token ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abcdefghijkl"),
        ("dsn postgresql://api_service:hunter2hunter2@db:5432/agent", "hunter2hunter2"),
        ("redis rediss://default:s3cr3tpass@kv:6379", "s3cr3tpass"),
        ("callback?code=abc123&password=pa55word", "pa55word"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", "MIIEow"),
    ],
)
def test_redaction(raw: str, leaked: str) -> None:
    assert leaked not in redact(raw)


def test_redaction_keeps_ordinary_text() -> None:
    text = "user alice@acme.test created ticket ABC-123 in 42 ms"
    assert redact(text) == text


# ------------------------------------------------------------------ JSON formatter


def _format(record: logging.LogRecord) -> dict[str, object]:
    return json.loads(JsonFormatter("agent-test").format(record))  # type: ignore[no-any-return]


def test_json_shape_with_context() -> None:
    org = uuid.uuid4()
    rid_token, org_token = request_id_var.set("req-12345678"), org_id_var.set(org)
    try:
        record = logging.makeLogRecord(
            {
                "name": "app.x",
                "levelno": logging.INFO,
                "levelname": "INFO",
                "msg": "hello %s",
                "args": ("world",),
                "sqlstate": "23505",
            }
        )
        entry = _format(record)
    finally:
        request_id_var.reset(rid_token)
        org_id_var.reset(org_token)
    assert entry["msg"] == "hello world"
    assert entry["level"] == "INFO"
    assert entry["service"] == "agent-test"
    assert entry["request_id"] == "req-12345678"
    assert entry["org_id"] == str(org)
    assert entry["sqlstate"] == "23505"
    assert "ts" in entry


def test_json_redacts_message_extras_and_exceptions() -> None:
    try:
        raise RuntimeError("failed with xoxb-0000000000-secretsecret")
    except RuntimeError:
        import sys

        record = logging.makeLogRecord(
            {"name": "app.x", "levelno": logging.ERROR, "levelname": "ERROR", "msg": "dsn %s",
             "args": ("postgresql://u:topsecretpw@h/d",), "exc_info": sys.exc_info(), "detail": "Bearer abcdefghijklmnop"}
        )  # fmt: skip
    text = JsonFormatter("agent-test").format(record)
    for secret in ("topsecretpw", "secretsecret", "abcdefghijklmnop"):
        assert secret not in text


# ------------------------------------------------------------------ request ids and access log


async def test_request_id_is_generated_and_echoed(client: httpx.AsyncClient) -> None:
    response = await client.get("/t/no-bind")
    assert len(response.headers["x-request-id"]) == 32


async def test_valid_incoming_request_id_is_kept(client: httpx.AsyncClient) -> None:
    response = await client.get("/t/no-bind", headers={"x-request-id": "render-abc-123456"})
    assert response.headers["x-request-id"] == "render-abc-123456"


@pytest.mark.parametrize("bad", ["short", "x" * 80, "has spaces in it", "evil\r\ninjected"])
async def test_malformed_incoming_request_id_is_replaced(client: httpx.AsyncClient, bad: str) -> None:
    try:
        response = await client.get("/t/no-bind", headers={"x-request-id": bad})
    except httpx.LocalProtocolError:
        return  # the client refuses to send header injection at all
    assert response.headers["x-request-id"] != bad


async def test_every_response_carries_the_request_id(client: httpx.AsyncClient, world: World) -> None:
    for method, path in (
        ("get", "/t/no-bind"),
        ("post", "/t/write-then-fail?marker=x"),
        ("post", "/t/write-then-crash?marker=x"),
        ("get", "/nope"),
    ):
        response = await client.request(method, path)
        assert "x-request-id" in response.headers, path


async def test_access_log_line_per_request(
    client: httpx.AsyncClient, world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.access")
    await client.get("/t/channels", headers={"x-org": str(world.acme.id)})
    (record,) = [r for r in caplog.records if r.name == "app.access"]
    assert record.http_status == 200  # type: ignore[attr-defined]
    assert record.http_path == "/t/channels"  # type: ignore[attr-defined]
    assert record.duration_ms >= 0  # type: ignore[attr-defined]


async def test_health_checks_are_not_logged_at_info(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.access")
    await client.get("/healthz")
    assert [r for r in caplog.records if r.name == "app.access"] == []


# ------------------------------------------------------------------ tracing


async def test_tenant_id_on_server_and_database_spans(client: httpx.AsyncClient, world: World) -> None:
    SPANS.clear()
    await client.get("/t/channels", headers={"x-org": str(world.acme.id)})
    spans = SPANS.get_finished_spans()
    server = [s for s in spans if s.kind.name == "SERVER"]
    db = [s for s in spans if s.attributes and s.attributes.get("db.system") == "postgresql"]
    assert server, "no server span"
    assert db, "no database spans"
    assert server[0].attributes is not None
    assert server[0].attributes.get(TENANT_ATTRIBUTE) == str(world.acme.id)
    # Binding = the pool's liveness check + set_config; those run before the tenant is known.
    # Every database span that starts after set_config finishes must carry the tenant.
    (bind,) = [s for s in db if "set_config" in str((s.attributes or {}).get("db.statement", ""))]
    assert bind.end_time is not None
    after_bind = [s for s in db if s.start_time is not None and s.start_time >= bind.end_time]
    assert len(after_bind) == 2
    assert all((s.attributes or {}).get(TENANT_ATTRIBUTE) == str(world.acme.id) for s in after_bind)


async def test_no_tenant_on_requests_that_do_not_bind(client: httpx.AsyncClient) -> None:
    SPANS.clear()
    await client.get("/t/no-bind")
    assert all(TENANT_ATTRIBUTE not in (s.attributes or {}) for s in SPANS.get_finished_spans())


async def test_spans_hold_no_query_strings_or_sql_parameters(client: httpx.AsyncClient, world: World) -> None:
    SPANS.clear()
    await client.get("/t/channels?code=oauth-secret-code&state=xyz", headers={"x-org": str(world.globex.id)})
    dump = json.dumps([dict(s.attributes or {}) for s in SPANS.get_finished_spans()])
    assert "oauth-secret-code" not in dump
    # set_config's argument (the org id) is a parameter: statement text only, never values.
    set_config = [
        s
        for s in SPANS.get_finished_spans()
        if "set_config" in str((s.attributes or {}).get("db.statement", ""))
    ]
    assert set_config
    assert str(world.globex.id) not in str((set_config[0].attributes or {}).get("db.statement"))

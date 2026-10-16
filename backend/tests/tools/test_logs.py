"""The observability interface: error signatures and grouping, windows and filters pushed to the
backend, and the Google Cloud Logging adapter through the executor against the stand-in (log
sources from the catalog only, another project refused, restricted sources, the vault's service
account tokens)."""

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio

from app.observability import Event, LogSource, ObservabilityError, Window, group, normalize, signature
from app.observability.gcp import build_filter, quoted
from app.vault_service import VaultError
from tests.connect import connect_logging, enable, new_connection, sql
from tests.stack import GCP_KEY, Stack, fakes_up
from tests.tools.test_integrations import Setup

pytestmark = pytest.mark.skipif(not fakes_up(), reason="stand-ins not reachable (run `make infra`)")
SOURCE = {"provider": "google_cloud_logging", "project": "acme-prod", "resource_type": "cloud_run_revision"}
TRACEBACK = """Traceback (most recent call last):
  File "/app/src/api/checkout.py", line 41, in checkout
    amount = total(cart.items)
  File "/app/src/pricing/cart.py", line 7, in total
    prices[n] = item.price
IndexError: list assignment index out of range"""


# ------------------------------------------------------------------ the interface (no backend)


def test_signatures_ignore_what_varies() -> None:
    sig, kind, top = signature("Unhandled exception in POST /checkout", TRACEBACK)
    assert (sig, kind, top) == (
        "IndexError: list assignment index out of range @ src/pricing/cart.py:7 in total",
        "IndexError",
        "src/pricing/cart.py:7 in total",
    )
    a = signature("timeout after 3021ms calling 10.0.0.12 for order 8f3e2a9c0b1d4e5f")[0]
    b = signature("timeout after 18ms calling 10.0.4.1 for order 0d9c8b7a6f5e4d3c")[0]
    assert a == b == "timeout after <n>ms calling <ip> for order <hex>"
    assert normalize("user 'ann' <ann@x.io> id 3fa85f64-5717-4562-b3fc-2c963f66afa6") == (
        "user <str> <<email>> id <uuid>"
    )
    assert signature("ValueError: bad cart c-41")[0] == "ValueError: bad cart c-<n>"


def test_events_group_by_signature_with_counts_and_samples() -> None:
    events = [Event(f"2026-10-16T0{n}:00:00Z", "ERROR", "boom", TRACEBACK, f"t{n}") for n in range(5)]
    events += [Event("2026-10-16T09:00:00Z", "WARNING", "slow upstream: tax-service took 1200ms")]
    groups = group(events)
    assert [(g.count, g.exception_type) for g in groups] == [(5, "IndexError"), (1, None)]
    assert (groups[0].first_seen, groups[0].last_seen) == ("2026-10-16T00:00:00Z", "2026-10-16T04:00:00Z")
    assert len(groups[0].samples) == 3


def test_windows_default_cap_and_stay_in_the_past() -> None:
    now = datetime(2026, 10, 16, 12, tzinfo=UTC)
    assert Window.parse(None, None, now) == Window(now - timedelta(hours=24), now)
    assert Window.parse("2026-10-16T06:00:00Z", "2026-10-17T00:00:00Z", now).end == now  # not the future
    with pytest.raises(ObservabilityError, match="at most 7 days"):
        Window.parse("2026-09-01T00:00:00Z", None, now)
    with pytest.raises(ObservabilityError, match="before its end"):
        Window.parse("2026-10-16T10:00:00Z", "2026-10-16T09:00:00Z", now)
    with pytest.raises(ObservabilityError, match="not a time"):
        Window.parse("yesterday", None, now)


def test_filters_are_built_for_the_backend_and_quote_values() -> None:
    source = LogSource("cart-service", "google_cloud_logging", "acme-prod", "cloud_run_revision")
    window = Window(datetime(2026, 10, 16, 6, tzinfo=UTC), datetime(2026, 10, 16, 12, tzinfo=UTC))
    assert build_filter(source, window, "ERROR", 'say "hi" \\ bye') == (
        'resource.type="cloud_run_revision" AND resource.labels.service_name="cart-service" AND severity>=ERROR '
        'AND timestamp>="2026-10-16T06:00:00Z" AND timestamp<="2026-10-16T12:00:00Z" AND (textPayload:"say \\"hi\\" '
        '\\\\ bye" OR jsonPayload.message:"say \\"hi\\" \\\\ bye" OR jsonPayload.exception:"say \\"hi\\" \\\\ bye")'
    )
    assert quoted('a" OR severity>=DEBUG OR "') == '"a\\" OR severity>=DEBUG OR \\""'  # stays one literal
    with pytest.raises(ObservabilityError, match="no log source"):
        LogSource.from_catalog("cart-service", {})


# ------------------------------------------------------------------ the adapter on the stand-in


@pytest_asyncio.fixture(loop_scope="session")
async def setup(stack: Stack) -> AsyncIterator[Setup]:
    acme = stack.env.world.acme
    enable(stack, acme, "slack")
    await connect_logging(stack, acme)
    sql(stack, "delete from app.services where org_id = %s and name in ('cart-service', 'web-app', 'payments')",
        (acme.id,))  # fmt: skip
    for name, source in (
        ("cart-service", SOURCE),
        ("web-app", SOURCE),
        ("payments", {**SOURCE, "project": "globex-prod"}),  # a project the account cannot read
    ):
        sql(stack, "insert into app.services (org_id, name, log_source) values (%s, %s, %s)",
            (acme.id, name, json.dumps(source)))  # fmt: skip
    async with httpx.AsyncClient(timeout=30) as http:
        yield Setup(stack, http)


@pytest.fixture(autouse=True)
def _close_tasks(stack: Stack) -> Iterator[None]:
    yield
    sql(stack, "update app.tasks set status = 'completed', finished_at = now() where status = 'running'")


def ago(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_events_of_a_catalog_service_grouped_by_signature(setup: Setup) -> None:
    bob = await setup.executor("bob")
    found = await bob.run(
        "google_cloud_logging_search_events",
        {"service": "Cart-Service", "start": ago(8), "severity": "ERROR"},
    )
    assert not found.is_error, found.text
    assert found.decision is not None
    assert (found.decision.outcome, found.decision.credential_mode) == ("allowed", "org")
    (top,) = found.data["groups"]
    assert (top["count"], top["exception_type"], top["top_frame"]) == (
        37,
        "IndexError",
        "src/pricing/cart.py:7 in total",
    )
    assert "x37" in found.text
    assert "cart_size=" in found.text  # samples with their labels
    assert "trace " in found.text
    (source,) = [s for s in bob.ctx.refs.by_ref.values() if s.kind == "logs"]
    assert (source.visibility, source.audience) == ("restricted", "google_cloud_logging:acme-prod")
    assert "console.cloud.google.com/logs/query" in str(source.permalink)
    noise = await bob.run("google_cloud_logging_search_events", {"service": "cart-service", "start": ago(8)})
    assert noise.data["total"] > top["count"]  # all severities: routine traffic too
    text = await bob.run("google_cloud_logging_search_events",
                         {"service": "web-app", "start": ago(30), "text": "slow search", "severity": "WARNING"})  # fmt: skip
    assert text.data["groups"][0]["signature"] == "slow search request: <n>ms"


async def test_error_groups_and_a_trace_from_a_sample(setup: Setup) -> None:
    bob = await setup.executor("bob")
    groups = await bob.run(
        "google_cloud_logging_list_error_groups", {"service": "cart-service", "start": ago(72)}
    )
    counts = sorted(g["count"] for g in groups.data["groups"])
    assert counts == [26, 37]  # the checkout errors and the webhook incident two days ago
    recent = await bob.run(
        "google_cloud_logging_list_error_groups", {"service": "cart-service", "start": ago(8)}
    )
    assert [g["count"] for g in recent.data["groups"]] == [37]
    found = await bob.run(
        "google_cloud_logging_search_events",
        {"service": "cart-service", "start": ago(8), "severity": "ERROR"},
    )
    trace_id = found.data["groups"][0]["samples"][0]["trace_id"]
    trace = await bob.run("google_cloud_logging_get_trace", {"service": "cart-service", "trace_id": trace_id})
    assert [s["name"] for s in trace.data["spans"]] == [
        "POST /checkout",
        "pricing.total",
        "GET tax-service /rates",
    ]
    bad = await bob.run("google_cloud_logging_get_trace", {"service": "cart-service", "trace_id": "../../x"})
    assert bad.is_error


async def test_only_what_the_catalog_points_at_is_read(setup: Setup) -> None:
    bob = await setup.executor("bob")
    unknown = await bob.run("google_cloud_logging_search_events", {"service": "billing"})
    assert unknown.is_error
    assert "No confirmed service named 'billing'" in unknown.text
    sql(setup.stack, "insert into app.services (org_id, name) values (%s, 'ledger')", (setup.acme.id,))
    try:
        nowhere = await bob.run("google_cloud_logging_search_events", {"service": "ledger"})
        assert "no log source for ledger" in nowhere.text
    finally:
        sql(setup.stack, "delete from app.services where name = 'ledger' and org_id = %s", (setup.acme.id,))
    other = await bob.run("google_cloud_logging_search_events", {"service": "payments"})
    assert other.is_error
    assert "refused access" in other.text
    assert "Globex" not in other.text
    wide = await bob.run(
        "google_cloud_logging_search_events", {"service": "cart-service", "start": ago(24 * 9)}
    )
    assert "at most 7 days" in wide.text


async def test_without_a_connection_there_are_no_logs(setup: Setup) -> None:
    sql(
        setup.stack,
        "update app.connections set status = 'revoked' where integration_key = 'google_cloud_logging'",
    )
    try:
        found = await (await setup.executor("bob")).run(
            "google_cloud_logging_search_events", {"service": "cart-service"}
        )
        assert found.denied
        assert "not connected" in found.text
    finally:
        await connect_logging(setup.stack, setup.acme)


async def test_the_audience_is_the_orgs_active_members(setup: Setup) -> None:
    from app.policy import Member
    from app.tools.base import Source

    bob = await setup.executor("bob")
    adapter = bob.registry.adapters["google_cloud_logging"]
    source = Source("logs:acme-prod:x", "", "cart-service logs", "restricted", "", kind="logs",
                    audience="google_cloud_logging:acme-prod")  # fmt: skip
    members = [
        Member(setup.acme.user, "bob@acme.test", "U0BOB", {}),
        Member(setup.acme.admin, "alice@acme.test", "U0ALICE", {}),
    ]
    assert await bob.audience(source, members)
    sql(setup.stack, "update app.org_memberships set status = 'removed' where id = %s", (setup.acme.user,))
    try:
        assert not await adapter.audience_ok(bob.ctx, None, source, members)  # type: ignore[attr-defined]
    finally:
        sql(setup.stack, "update app.org_memberships set status = 'active' where id = %s", (setup.acme.user,))


async def test_the_vault_checks_and_keeps_service_account_keys(setup: Setup) -> None:
    stack, acme = setup.stack, setup.acme
    conn = new_connection(stack, acme, "google_cloud_logging")
    for bad, why in (("{}", "not a service account key"), ("nope", "not a JSON"),
                     (json.dumps({**json.loads(GCP_KEY), "private_key": "x"}), "cannot be read")):  # fmt: skip
        with pytest.raises(VaultError, match=why) as refused:
            await stack.vault.register_service_account(acme.id, conn, bad)
        assert refused.value.code == "bad_request"
    # A key file's own token endpoint is ignored: the vault always asks ours.
    redirected = json.dumps({**json.loads(GCP_KEY), "token_uri": "http://169.254.169.254/token"})
    result = await stack.vault.register_service_account(acme.id, conn, redirected)
    assert (result["external_account_id"], result["project_id"]) == (
        "agent-reader@acme-prod.iam.gserviceaccount.com", "acme-prod",
    )  # fmt: skip
    with pytest.raises(VaultError, match="only static secrets"):
        await stack.vault.read(acme.id, conn)  # the key never leaves the vault
    credential = await stack.vault.credential(acme.id, conn)
    assert credential.kind == "gcp_service_account"
    assert credential.details == {
        "project_id": "acme-prod",
        "account": "agent-reader@acme-prod.iam.gserviceaccount.com",
    }
    assert "PRIVATE KEY" not in json.dumps(credential.to_dict())
    other = json.loads(GCP_KEY)
    other["client_email"] = "intruder@evil-project.iam.gserviceaccount.com"
    with pytest.raises(VaultError) as refused_issuer:
        await stack.vault.register_service_account(acme.id, conn, json.dumps(other))
    assert refused_issuer.value.code == "provider"  # Google refused the assertion

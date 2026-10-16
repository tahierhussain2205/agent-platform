"""Alerts (plan phase 13): the rules, the latency histogram, the figures and the delivery."""

import asyncio
import json
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, ClassVar

import psycopg
import pytest

from app import kv
from app.alerts import (
    Latency,
    OrgFigures,
    _minute_key,
    check,
    evaluate,
    ingress_latency,
    org_figures,
    record_ingress_latency,
)
from app.settings import Settings
from tests.stack import kv_url
from tests.world import World, tx

KV_DB = 10


def settings(**kw: Any) -> Settings:
    return Settings(redis_url=kv_url(KV_DB), **kw)


def figures(**kw: Any) -> OrgFigures:
    base: dict[str, Any] = {
        "org_id": uuid.uuid4(), "name": "Acme", "finished": 0, "failed": 0, "queued": 0,
        "oldest_queued_seconds": None, "budget_stops": 0, "month_tokens": 0, "month_limit": None,
        "schedules_paused": 0,
    }  # fmt: skip
    return OrgFigures(**{**base, **kw})


# ------------------------------------------------------------------ rules


def test_nothing_to_say_below_every_threshold() -> None:
    s = settings()
    quiet = figures(
        finished=20, failed=4, queued=10, oldest_queued_seconds=300, month_tokens=10, month_limit=100
    )
    assert evaluate(s, [quiet], Latency(500, 1000, 2500)) == []
    assert evaluate(s, [], None) == []


def test_failure_rate_needs_enough_tasks_and_names_the_org_when_others_are_fine() -> None:
    s = settings()
    assert evaluate(s, [figures(finished=7, failed=7)], None) == []  # too few to mean anything
    (overall,) = evaluate(s, [figures(finished=8, failed=2)], None)
    assert overall.key == "failure-rate"
    assert "2 of 8 (25%)" in overall.text
    bad, good = figures(name="Acme", finished=10, failed=6), figures(name="Globex", finished=10, failed=1)
    alerts = evaluate(s, [bad, good], None)
    assert [a.key for a in alerts] == ["failure-rate", f"failure-rate-{bad.org_id}"]
    assert "Acme" in alerts[1].text
    assert "6 of 10 (60%)" in alerts[1].text


def test_queue_budget_limit_and_schedule_conditions() -> None:
    s = settings()
    org = figures(queued=60, oldest_queued_seconds=11 * 60, budget_stops=2, month_tokens=100, month_limit=100,
                  schedules_paused=1)  # fmt: skip
    alerts = {a.key: a.text for a in evaluate(s, [org], None)}
    assert set(alerts) == {
        "queue-depth", f"queue-age-{org.org_id}", f"budget-stops-{org.org_id}",
        f"monthly-limit-{org.org_id}", f"schedules-paused-{org.org_id}",
    }  # fmt: skip
    assert "60 tasks are waiting" in alerts["queue-depth"]
    assert "the oldest for 11 min" in alerts[f"queue-age-{org.org_id}"]
    assert "100 of 100 tokens" in alerts[f"monthly-limit-{org.org_id}"]


def test_slow_acknowledgments_alert_only_with_enough_samples() -> None:
    s = settings()
    assert evaluate(s, [], Latency(19, float("inf"), float("inf"))) == []  # a cold start alone
    (slow,) = evaluate(s, [], Latency(200, 2000, 3000))
    assert slow.key == "ingress-latency"
    assert "p95 ≤ 2,000 ms" in slow.text
    assert "p99 ≤ 3,000 ms" in slow.text
    (worse,) = evaluate(s, [], Latency(200, 1000, float("inf")))
    assert "over 10,000 ms" in worse.text
    assert (
        evaluate(settings(alert_ingress_p95_ms=2500, alert_ingress_p99_ms=5000), [], Latency(200, 2000, 3000))
        == []
    )


# ------------------------------------------------------------------ latency histogram


async def test_latency_percentiles_from_the_histogram() -> None:
    client = kv.make_client(kv_url(KV_DB))
    at = datetime(2001, 1, 1, 0, 14, tzinfo=UTC)  # far from any live minute
    earlier = datetime(2001, 1, 1, 0, 1, tzinfo=UTC)
    outside = datetime(2000, 12, 31, 23, 59, tzinfo=UTC)  # 15 minutes before: not in the window
    try:
        for ms, n, when in (
            (80, 60, at),
            (80, 30, earlier),
            (1800, 6, at),
            (4000, 4, at),
            (9000, 50, outside),
        ):
            for _ in range(n):
                await record_ingress_latency(client, ms, when)
        latency = await ingress_latency(client, at)
        assert latency == Latency(100, 2000.0, 5000.0)
        assert 0 < await client.ttl(_minute_key(at)) <= 1800
        await record_ingress_latency(None, 5)  # no Key Value: nothing to do, no error
    finally:
        await client.delete(*(_minute_key(t) for t in (at, earlier, outside)))
        await client.aclose()


# ------------------------------------------------------------------ figures


def test_figures_count_each_org_and_only_the_maintenance_role_reads_them(
    fresh: World, maintenance: str
) -> None:
    acme, globex = fresh.acme, fresh.globex
    with psycopg.connect(fresh.url) as conn:  # the owner: sets up any org's rows
        for status, error, minutes in (("failed", "boom", 5), ("failed", "boom", 90), ("completed", "stopped: budget", 10),
                                       ("completed", None, 20)):  # fmt: skip
            conn.execute(
                "insert into app.tasks (org_id, session_id, requested_by, trigger, model_tier, status, error, "
                "finished_at) values (%s, %s, %s, 'mention', 'standard', %s, %s, now() - make_interval(mins => %s))",
                (acme.id, acme.ids["sessions"], acme.user, status, error, minutes),
            )
        conn.execute(
            "insert into app.tasks (org_id, session_id, requested_by, trigger, model_tier, status, created_at) "
            "values (%s, %s, %s, 'mention', 'standard', 'queued', now() - interval '12 minutes')",
            (acme.id, acme.ids["sessions"], acme.user),
        )
        conn.execute(
            "update app.org_settings set org_monthly_token_limit = 1000 where org_id = %s", (acme.id,)
        )
        conn.execute(
            "insert into app.usage_events (org_id, membership_id, kind, model, input_tokens, output_tokens) "
            "values (%s, %s, 'llm', 'm', 900, 200)",
            (acme.id, acme.user),
        )
        conn.execute(
            "update app.schedules set status = 'paused', pause_reason = 'access_lost' where org_id = %s",
            (acme.id,),
        )
        conn.commit()
    with psycopg.connect(maintenance) as conn:
        rows = {f.org_id: f for f in org_figures(conn)}
    a = rows[acme.id]
    # The world's own completed task counts too; the failure 90 minutes ago does not.
    assert (a.failed, a.queued, a.budget_stops, a.schedules_paused) == (1, 1, 1, 1)
    assert a.finished >= 3
    assert a.oldest_queued_seconds is not None
    assert 11 * 60 < a.oldest_queued_seconds < 13 * 60
    assert a.month_tokens >= 1100
    assert a.month_limit == 1000
    g = rows[globex.id]
    assert (g.failed, g.queued, g.budget_stops, g.schedules_paused) == (0, 0, 0, 0)
    with (
        tx(fresh.url, role="app_user", org=acme.id) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("select * from app.maintenance_alert_figures()")


# ------------------------------------------------------------------ delivery


class Webhook(BaseHTTPRequestHandler):
    received: ClassVar[list[dict[str, Any]]] = []
    status: ClassVar[int] = 200

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        Webhook.received.append(json.loads(self.rfile.read(length)))
        self.send_response(Webhook.status)
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def webhook() -> Iterator[str]:
    Webhook.received, Webhook.status = [], 200
    server = HTTPServer(("127.0.0.1", 0), Webhook)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/hook"
    server.shutdown()


def _clear_dedupe() -> None:
    async def clear() -> None:
        client = kv.make_client(kv_url(KV_DB))
        try:
            found = [k async for k in client.scan_iter(match=kv.global_key("alert", "*"))]
            if found:
                await client.delete(*found)
        finally:
            await client.aclose()

    asyncio.run(clear())


def test_each_alert_is_sent_once_an_hour_and_retried_when_the_webhook_fails(
    fresh: World, maintenance: str, webhook: str
) -> None:
    _clear_dedupe()
    with psycopg.connect(fresh.url) as conn:
        conn.execute(
            "update app.org_settings set org_monthly_token_limit = 10 where org_id = %s", (fresh.globex.id,)
        )
        conn.execute(
            "insert into app.usage_events (org_id, membership_id, kind, model, input_tokens) "
            "values (%s, %s, 'llm', 'm', 50)",
            (fresh.globex.id, fresh.globex.user),
        )
        conn.commit()
    s = settings(alert_webhook_url=webhook)
    Webhook.status = 500
    with psycopg.connect(maintenance) as conn:
        assert check(conn, s) == "1 alert(s), 1 new, logged"
    Webhook.status = 200
    with psycopg.connect(maintenance) as conn:
        assert check(conn, s) == "1 alert(s), 1 new, sent"  # the failed delivery was released
        assert check(conn, s) == "1 alert(s), 0 new, logged"  # deduplicated for the hour
    assert len(Webhook.received) == 2
    text = Webhook.received[-1]["text"]
    assert text.startswith("Agent platform alerts (")
    assert f"Monthly limit reached for Globex Inc ({fresh.globex.id}): " in text
    assert text.endswith(" of 10 tokens.")
    with psycopg.connect(maintenance) as conn:  # no webhook: logged only
        assert check(conn, settings()) == "1 alert(s), 0 new, logged (no webhook)"
    _clear_dedupe()

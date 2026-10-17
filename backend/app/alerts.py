"""Alerts (plan phase 13): a maintenance job that checks the platform's health every run.

Conditions: task failure rate (last hour, overall and per org), queue depth and the oldest queued
task, Slack ingress acknowledgment latency (p95 and p99 of the last 15 minutes), budget stops and
orgs at their monthly limit, schedules paused by the system. Thresholds are settings. Each alert
goes to ``ALERT_WEBHOOK_URL`` (Slack incoming webhook format) at most once per condition per hour
(deduplicated in Key Value) and is always logged. Figures only: no tenant content leaves.

The Slack ingress records each acknowledgment's latency into a per minute histogram in Key Value
(``record_ingress_latency``), after the response is sent.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import psycopg

from app import kv
from app.settings import Settings

if TYPE_CHECKING:
    import redis.asyncio as redis

log = logging.getLogger(__name__)

# Histogram bucket upper bounds in milliseconds; Slack gives up after 3 seconds.
BUCKETS_MS = (50, 100, 250, 500, 1000, 1500, 2000, 2500, 3000, 5000, 10000)
OVER = "inf"
HISTOGRAM_TTL = 1800
LATENCY_WINDOW_MINUTES = 15
MIN_LATENCY_SAMPLES = 20  # a cold start alone is not an incident
DEDUPE_SECONDS = 3600


def _minute_key(at: datetime) -> str:
    return kv.global_key("ingress-ms", at.astimezone(UTC).strftime("%Y%m%d%H%M"))


def _bucket(ms: float) -> str:
    return next((str(b) for b in BUCKETS_MS if ms <= b), OVER)


async def record_ingress_latency(client: "redis.Redis | None", ms: float, at: datetime | None = None) -> None:
    """Count one acknowledgment in this minute's histogram. Never fails the caller."""
    if client is None:
        return
    key = _minute_key(at or datetime.now(UTC))
    try:
        async with client.pipeline(transaction=False) as pipe:
            pipe.hincrby(key, _bucket(ms), 1)
            pipe.expire(key, HISTOGRAM_TTL)
            await pipe.execute()
    except Exception:
        log.warning("could not record ingress latency", exc_info=True)


@dataclass(frozen=True)
class Latency:
    samples: int
    p95_ms: float  # bucket upper bounds; inf when beyond the last bucket
    p99_ms: float


async def ingress_latency(client: "redis.Redis", now: datetime | None = None) -> Latency:
    now = now or datetime.now(UTC)
    counts: dict[str, int] = {}
    for i in range(LATENCY_WINDOW_MINUTES):
        raw = await client.hgetall(_minute_key(now - timedelta(minutes=i)))
        for field, value in raw.items():
            name = field.decode() if isinstance(field, bytes) else str(field)
            counts[name] = counts.get(name, 0) + int(value)
    total = sum(counts.values())
    order = [*(str(b) for b in BUCKETS_MS), OVER]

    def percentile(q: float) -> float:
        if total == 0:
            return 0.0
        seen = 0
        for name in order:
            seen += counts.get(name, 0)
            if seen >= q * total:
                return float("inf") if name == OVER else float(name)
        return float("inf")

    return Latency(total, percentile(0.95), percentile(0.99))


@dataclass(frozen=True)
class Alert:
    key: str  # one condition: deduplicated per hour
    text: str


@dataclass(frozen=True)
class OrgFigures:
    org_id: Any
    name: str
    finished: int
    failed: int
    queued: int
    oldest_queued_seconds: int | None
    budget_stops: int
    month_tokens: int
    month_limit: int | None
    schedules_paused: int


def _rate(failed: int, finished: int) -> str:
    return f"{failed} of {finished} ({failed / finished:.0%})"


def evaluate(settings: Settings, orgs: Sequence[OrgFigures], latency: Latency | None) -> list[Alert]:
    """The alerts these figures call for (pure: thresholds from settings)."""
    alerts: list[Alert] = []

    def add(key: str, text: str) -> None:
        alerts.append(Alert(key, text))

    finished = sum(o.finished for o in orgs)
    failed = sum(o.failed for o in orgs)
    if finished >= settings.alert_min_finished and failed / finished >= settings.alert_failure_rate:
        add("failure-rate", f"Task failures: {_rate(failed, finished)} tasks failed in the last hour.")
    queued = sum(o.queued for o in orgs)
    if queued >= settings.alert_queue_depth:
        add("queue-depth", f"Queue depth: {queued} tasks are waiting.")
    for o in orgs:
        who = f"{o.name} ({o.org_id})"
        if (
            o.finished >= settings.alert_min_finished
            and o.failed / o.finished >= settings.alert_failure_rate
            and o.failed < failed  # when this org is all of it, the overall alert says so
        ):
            add(f"failure-rate-{o.org_id}", f"Task failures for {who}: {_rate(o.failed, o.finished)}.")
        oldest = o.oldest_queued_seconds
        if oldest is not None and oldest >= settings.alert_queue_age_minutes * 60:
            add(
                f"queue-age-{o.org_id}",
                f"Queued too long for {who}: {o.queued} waiting, the oldest for {oldest // 60} min.",
            )
        if o.budget_stops:
            add(
                f"budget-stops-{o.org_id}",
                f"Budget stops for {who}: {o.budget_stops} task(s) in the last hour.",
            )
        if o.month_limit is not None and o.month_tokens >= o.month_limit:
            add(
                f"monthly-limit-{o.org_id}",
                f"Monthly limit reached for {who}: {o.month_tokens:,} of {o.month_limit:,} tokens.",
            )
        if o.schedules_paused:
            add(
                f"schedules-paused-{o.org_id}",
                f"Schedules paused for {who}: {o.schedules_paused} in the last hour (access or budget).",
            )
    slow = (
        latency is not None
        and latency.samples >= MIN_LATENCY_SAMPLES
        and (latency.p95_ms > settings.alert_ingress_p95_ms or latency.p99_ms > settings.alert_ingress_p99_ms)
    )
    if slow and latency is not None:

        def ms(value: float) -> str:
            return f"over {BUCKETS_MS[-1]:,} ms" if value == float("inf") else f"≤ {value:,.0f} ms"

        add(
            "ingress-latency",
            f"Slack acknowledgments are slow: p95 {ms(latency.p95_ms)}, p99 {ms(latency.p99_ms)} "
            f"over {latency.samples} requests in the last {LATENCY_WINDOW_MINUTES} min.",
        )
    return alerts


def org_figures(conn: psycopg.Connection) -> list[OrgFigures]:
    rows = conn.execute("select * from app.maintenance_alert_figures()").fetchall()
    return [OrgFigures(*row) for row in rows]


async def _deliver(settings: Settings, alerts: list[Alert]) -> tuple[list[Alert], bool]:
    """Alerts not sent in the last hour, and whether the webhook took them."""
    fresh = alerts
    client = kv.make_client(settings.redis_url.get_secret_value()) if settings.redis_url else None
    try:
        if client is not None:
            fresh = [
                a for a in alerts if await kv.claim(client, kv.global_key("alert", a.key), DEDUPE_SECONDS)
            ]
        if not fresh or settings.alert_webhook_url is None:
            return fresh, False
        text = "\n".join(f"• {a.text}" for a in fresh)
        try:
            async with httpx.AsyncClient(timeout=10.0) as http:
                response = await http.post(
                    settings.alert_webhook_url.get_secret_value(),
                    json={"text": f"Agent platform alerts ({settings.env}):\n{text}"},
                )
            if response.status_code >= 300:
                raise httpx.HTTPStatusError("webhook refused", request=response.request, response=response)
        except httpx.HTTPError as exc:
            log.warning("alert webhook failed", extra={"error": type(exc).__name__})
            if client is not None:  # let the next run try again
                for a in fresh:
                    await kv.release(client, kv.global_key("alert", a.key))
            return fresh, False
        return fresh, True
    finally:
        if client is not None:
            await client.aclose()


async def _latency(settings: Settings) -> Latency | None:
    if settings.redis_url is None:
        return None
    client = kv.make_client(settings.redis_url.get_secret_value())
    try:
        return await ingress_latency(client)
    finally:
        await client.aclose()


def check(conn: psycopg.Connection, settings: Settings) -> str:
    orgs = org_figures(conn)
    alerts = evaluate(settings, orgs, asyncio.run(_latency(settings)))
    if not alerts:
        return "all clear"
    fresh, sent = asyncio.run(_deliver(settings, alerts))
    for a in alerts:
        log.warning("alert: %s", a.text, extra={"alert": a.key, "new": a in fresh})
    where = "sent" if sent else ("logged (no webhook)" if settings.alert_webhook_url is None else "logged")
    return f"{len(alerts)} alert(s), {len(fresh)} new, {where}"


def alerts_job(settings: Settings) -> tuple[str, Any]:
    def run(conn: psycopg.Connection) -> str:
        return check(conn, settings)

    return ("alerts", run)

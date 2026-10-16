"""Data lifecycle (plan phase 13; spec 12.5 and 16): retention and offboarding.

**Retention** keeps each org's data for its ``retention_days`` (90 by default, 1 to 3650). Per org, in
that org's own context (row level security applies to the maintenance role as to any other), it
deletes finished tasks older than the retention with everything hanging off them (steps,
approvals, external actions, links, artifacts, sandboxes), artifacts past ``retain_until``, and
sessions idle past the retention; objects of deleted artifacts go from object storage first.
Usage events and the audit log are kept by monthly partition (``USAGE_RETENTION_MONTHS``,
``AUDIT_RETENTION_MONTHS``); the daily usage rollup stays.

**Offboarding** deletes an org marked ``offboarding`` once its grace period
(``OFFBOARDING_GRACE_DAYS``) has passed, across every store: its Temporal schedules, its objects
(tenant prefix), its Key Value keys (tenant prefix), then its rows (one security definer function;
every tenant table cascades from ``orgs``). Provider tokens die with their ciphertext; the
providers' own installations (Slack app, GitHub App) are removed by the customer (runbook).
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

import psycopg

from app.settings import Settings

log = logging.getLogger(__name__)
TERMINAL = ("completed", "failed", "cancelled")


@dataclass
class Retained:
    tasks: int = 0
    artifacts: int = 0
    sessions: int = 0
    objects: int = 0
    partitions: int = 0
    failed_objects: int = 0

    def __str__(self) -> str:
        return (f"deleted {self.tasks} task(s), {self.artifacts} artifact(s), {self.sessions} session(s), "
                f"{self.objects} object(s), {self.partitions} partition(s)"
                + (f"; {self.failed_objects} object(s) could not be deleted"
                   if self.failed_objects else ""))  # fmt: skip


def _store(settings: Settings) -> Any:
    from app.storage import store_from_settings

    return store_from_settings(settings)


def apply_retention(conn: psycopg.Connection, settings: Settings, store: Any = None) -> Retained:
    """One pass over every org that is not offboarding (offboarding deletes everything anyway)."""
    done = Retained()
    store = store if store is not None else _store(settings)
    orgs = conn.execute(
        "select org_id, retention_days from app.maintenance_orgs() where status <> 'offboarding'"
    ).fetchall()
    for org_id, days in orgs:
        cutoff = timedelta(days=int(days))
        conn.execute("select set_config('app.org_id', %s, true)", (str(org_id),))
        old_tasks = (
            "select id from app.tasks where status = any(%(terminal)s) "
            "and coalesce(finished_at, created_at) < now() - %(cutoff)s"
        )
        params: dict[str, Any] = {"terminal": list(TERMINAL), "cutoff": cutoff}
        refs = [
            r[0]
            for r in conn.execute(
                f"select storage_ref from app.artifacts where storage_ref is not null "  # noqa: S608 - fixed SQL
                f"and (retain_until < now() or task_id in ({old_tasks}))",
                params,
            ).fetchall()
        ]
        failed: list[str] = []  # objects that could not be deleted keep their rows (and tasks): retried
        for ref in refs:
            if store is None:
                failed.append(ref)
                continue
            try:
                asyncio.run(store.delete(UUID(str(org_id)), ref))
                done.objects += 1
            except Exception:
                log.warning("could not delete an expired object", extra={"org": str(org_id)}, exc_info=True)
                failed.append(ref)
        done.failed_objects += len(failed)
        params["failed"] = failed
        done.artifacts += conn.execute(
            "delete from app.artifacts where retain_until < now() "
            "and (storage_ref is null or storage_ref <> all(%(failed)s))",
            params,
        ).rowcount
        done.tasks += conn.execute(
            f"delete from app.tasks t where t.id in ({old_tasks}) and not exists "  # noqa: S608 - fixed SQL
            "(select 1 from app.artifacts a where a.task_id = t.id and a.storage_ref = any(%(failed)s))",
            params,
        ).rowcount
        done.sessions += conn.execute(
            "delete from app.sessions s where s.last_activity_at < now() - %(cutoff)s and not exists "
            "(select 1 from app.tasks t where t.session_id = s.id and t.status <> all(%(terminal)s))",
            params,
        ).rowcount
    conn.execute("select set_config('app.org_id', '', true)")
    for parent, months in (("usage_events", settings.usage_retention_months),
                           ("audit_log", settings.audit_retention_months)):  # fmt: skip
        row = conn.execute("select app.maintenance_drop_partitions(%s, %s)", (parent, months)).fetchone()
        done.partitions += int(row[0]) if row else 0
    return done


@dataclass
class Offboarded:
    org_id: UUID
    schedules: int = 0
    objects: int = 0
    keys: int = 0
    deleted: bool = False
    notes: list[str] = field(default_factory=list)


async def _forget_outside_postgres(org_id: UUID, schedule_ids: list[UUID], settings: Settings) -> Offboarded:
    from app import kv
    from app.schedules.temporal import temporal_id

    result = Offboarded(org_id)
    if schedule_ids:
        from temporalio.service import RPCError, RPCStatusCode

        from app.temporal import connect_temporal

        client = await connect_temporal(settings, attempts=3, delay=0.5)
        for schedule_id in schedule_ids:
            try:
                await client.get_schedule_handle(temporal_id(schedule_id)).delete()
                result.schedules += 1
            except RPCError as exc:
                if exc.status != RPCStatusCode.NOT_FOUND:
                    raise
    store = _store(settings)
    if store is not None:
        result.objects = await store.delete_tenant(org_id)
    else:
        result.notes.append("no object storage configured")
    if settings.redis_url is not None:
        client_kv = kv.make_client(settings.redis_url.get_secret_value())
        try:
            prefix = kv.tenant_key(org_id, "x")[:-1]  # ap:t:<org>:
            batch: list[str] = []
            async for key in client_kv.scan_iter(match=f"{prefix}*", count=500):
                batch.append(key)
                if len(batch) >= 500:
                    result.keys += await client_kv.delete(*batch)
                    batch = []
            if batch:
                result.keys += await client_kv.delete(*batch)
        finally:
            await client_kv.aclose()
    else:
        result.notes.append("no Key Value configured")
    return result


def offboard(conn: psycopg.Connection, settings: Settings, org_id: UUID, grace: timedelta) -> Offboarded:
    """Delete one offboarding org across every store, once its grace period has passed."""
    row = conn.execute(
        "select status, offboarding_since from app.maintenance_orgs() where org_id = %s", (org_id,)
    ).fetchone()
    if row is None or row[0] != "offboarding":
        result = Offboarded(org_id)
        result.notes.append("not offboarding: nothing deleted")
        return result
    conn.execute("select set_config('app.org_id', %s, true)", (str(org_id),))
    schedule_ids = [r[0] for r in conn.execute("select id from app.schedules").fetchall()]
    conn.execute("select set_config('app.org_id', '', true)")
    if row[1] is not None and grace > timedelta(0):
        ready = conn.execute("select %s <= now() - %s", (row[1], grace)).fetchone()
        if not ready or not ready[0]:
            result = Offboarded(org_id)
            result.notes.append("in its grace period")
            return result
    result = asyncio.run(_forget_outside_postgres(org_id, schedule_ids, settings))
    deleted = conn.execute("select app.maintenance_offboard_org(%s, %s)", (org_id, grace)).fetchone()
    result.deleted = bool(deleted and deleted[0])
    log.info("org offboarded", extra={"org": str(org_id), "schedules": result.schedules,
                                      "objects": result.objects, "keys": result.keys,
                                      "deleted": result.deleted})  # fmt: skip
    return result


def retention_job(settings: Settings) -> tuple[str, Any]:
    def run(conn: psycopg.Connection) -> str:
        return str(apply_retention(conn, settings))

    return ("retention", run)


def offboarding_job(settings: Settings) -> tuple[str, Any]:
    def run(conn: psycopg.Connection) -> str:
        grace = timedelta(days=settings.offboarding_grace_days)
        due = [
            r[0]
            for r in conn.execute(
                "select org_id from app.maintenance_orgs() where status = 'offboarding' "
                "and offboarding_since <= now() - %s",
                (grace,),
            ).fetchall()
        ]
        if not due:
            return "no org to offboard"
        results = [offboard(conn, settings, org, grace) for org in due]
        return "; ".join(
            f"{r.org_id}: {'deleted' if r.deleted else 'kept'} ({r.schedules} schedule(s), "
            f"{r.objects} object(s), "
            f"{r.keys} key(s){', ' + ', '.join(r.notes) if r.notes else ''})"
            for r in results
        )

    return ("offboarding", run)

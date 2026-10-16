"""Temporal Schedules mirror ``app.schedules`` (the source of truth).

``sync_org`` makes Temporal match every row whose ``revision`` differs from its ``synced_revision``
and marks a row synced only if it did not change meanwhile, so a concurrent change is never lost:
it simply stays unsynced for the next pass (after the next change, a sync workflow, or the
maintenance job).
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from temporalio.service import RPCError, RPCStatusCode

from app.agent.types import ReportInput
from app.db.tenant import tenant_tx
from app.settings import Settings

log = logging.getLogger(__name__)
RUN_TIMEOUT = timedelta(hours=2)


def temporal_id(schedule_id: Any) -> str:
    return f"schedule-{schedule_id}"


@dataclass(frozen=True)
class Row:
    id: str
    org_id: str
    status: str
    cron: str
    timezone: str
    pause_reason: str | None
    revision: int


def build(row: Row, settings: Settings) -> Schedule:
    """Runs start ``ScheduledReportWorkflow``; Temporal appends the scheduled time to the workflow id,
    so each run has its own (and its own task)."""
    return Schedule(
        action=ScheduleActionStartWorkflow(
            "ScheduledReportWorkflow",
            ReportInput(row.org_id, row.id),
            id=f"report-{row.id}",
            task_queue=settings.temporal_task_queue,
            execution_timeout=RUN_TIMEOUT,
        ),
        spec=ScheduleSpec(
            cron_expressions=[row.cron],
            time_zone_name=row.timezone,
            jitter=timedelta(seconds=settings.schedule_jitter_seconds),
        ),
        policy=SchedulePolicy(
            overlap=ScheduleOverlapPolicy.SKIP,
            catchup_window=timedelta(minutes=settings.schedule_catchup_minutes),
        ),
        state=ScheduleState(paused=row.status != "active", note=row.pause_reason or ""),
    )


async def apply(client: Client, row: Row, settings: Settings) -> None:
    """Create, update, pause, unpause or delete the Temporal Schedule; idempotent."""
    handle = client.get_schedule_handle(temporal_id(row.id))
    if row.status == "deleted":
        try:
            await handle.delete()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
        return
    schedule = build(row, settings)
    try:
        await client.create_schedule(temporal_id(row.id), schedule)
        return
    except ScheduleAlreadyRunningError:
        pass

    def replace(_: ScheduleUpdateInput) -> ScheduleUpdate:
        return ScheduleUpdate(schedule=schedule)

    await handle.update(replace)
    # The paused state is changed explicitly: an update does not reliably carry it.
    if row.status == "active":
        await handle.unpause(note="active")
    else:
        await handle.pause(note=row.pause_reason or "paused")


async def sync_org(pool: Any, client: Client, settings: Settings, org_id: Any) -> tuple[int, int]:
    """Make Temporal match the org's unsynced schedules. Returns (synced, failed)."""
    async with tenant_tx(pool, org_id) as conn:
        rows = [
            Row(str(r[0]), str(r[1]), r[2], r[3], r[4], r[5], r[6])
            for r in await (
                await conn.execute(
                    "select id, org_id, status, cron, timezone, pause_reason, revision from app.schedules "
                    "where synced_revision is distinct from revision order by updated_at limit 200"
                )
            ).fetchall()
        ]
    synced = failed = 0
    for row in rows:
        try:
            await apply(client, row, settings)
        except Exception:  # one schedule must not stop the others; it stays unsynced
            log.warning("schedule sync failed", extra={"schedule_id": row.id}, exc_info=True)
            failed += 1
            continue
        async with tenant_tx(pool, org_id) as conn:
            await conn.execute(
                "update app.schedules set synced_revision = %s where id = %s and revision = %s",
                (row.revision, row.id, row.revision),
            )
        synced += 1
    return synced, failed


async def start_sync(client: Client | None, settings: Settings, org_id: Any) -> None:
    """Start ``ScheduleSyncWorkflow`` for an org after a change that may pause or alter schedules.
    Best effort: the maintenance job finds whatever this misses."""
    if client is None:
        return
    try:
        await client.start_workflow(
            "ScheduleSyncWorkflow",
            str(org_id),
            id=f"schedule-sync-{org_id}-{uuid.uuid4().hex[:12]}",
            task_queue=settings.temporal_task_queue,
            execution_timeout=timedelta(minutes=10),
        )
    except Exception:
        log.warning("could not start a schedule sync", extra={"org_id": str(org_id)}, exc_info=True)

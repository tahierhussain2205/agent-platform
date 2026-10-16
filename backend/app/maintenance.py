"""Maintenance jobs (``app maintenance``), run daily by the Render cron job.

Connects as ``maintenance_service`` (role ``app_maintenance``). Work that crosses tenants goes
through security definer functions, so it does not depend on BYPASSRLS (see migration 001 notes).
Each job is isolated: one failure is reported and the rest still run.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from app.settings import Settings

log = logging.getLogger(__name__)

PARTITION_MONTHS_AHEAD = 3


@dataclass(frozen=True)
class JobResult:
    name: str
    ok: bool
    detail: str
    seconds: float


def ensure_partitions(conn: psycopg.Connection) -> str:
    row = conn.execute("select app.ensure_monthly_partitions(%s)", (PARTITION_MONTHS_AHEAD,)).fetchone()
    assert row is not None
    return f"created {row[0]} partition(s), {PARTITION_MONTHS_AHEAD} months ahead covered"


Job = tuple[str, Callable[[psycopg.Connection], str]]

JOBS: list[Job] = [
    ("partitions", ensure_partitions),
]


def run_all(dsn: str, jobs: Sequence[Job] | None = None) -> list[JobResult]:
    results: list[JobResult] = []
    with psycopg.connect(dsn, autocommit=True, prepare_threshold=None) as conn:
        for name, job in jobs if jobs is not None else JOBS:
            started = time.monotonic()
            try:
                with conn.transaction():
                    detail = job(conn)
                results.append(JobResult(name, True, detail, time.monotonic() - started))
            except Exception as exc:  # one failing job must not stop the others
                log.exception("maintenance job %s failed", name)
                results.append(JobResult(name, False, type(exc).__name__, time.monotonic() - started))
    return results


def slack_reconcile_job(settings: Settings) -> Job:
    """Start a membership reconciliation per active Slack workspace (events can be missed).
    Workflow ids are hourly, so overlapping cron runs do not duplicate work."""

    def run(conn: psycopg.Connection) -> str:
        rows = conn.execute(
            "select org_id, workspace_id, external_id from app.maintenance_active_workspaces() where "
            "platform = 'slack'"
        ).fetchall()
        if not rows:
            return "no active workspaces"
        hour = datetime.now(UTC).strftime("%Y%m%d%H")

        async def start_all() -> int:
            from temporalio.exceptions import WorkflowAlreadyStartedError

            from app.temporal import connect_temporal
            from app.workflows.slack_types import SyncRequest

            client = await connect_temporal(settings, attempts=3, delay=0.5)
            started = 0
            for org_id, workspace_id, team_id in rows:
                try:
                    await client.start_workflow(
                        "SyncWorkspaceWorkflow",
                        SyncRequest(str(org_id), str(workspace_id), team_id),
                        id=f"slack-reconcile-{workspace_id}-{hour}",
                        task_queue=settings.temporal_task_queue,
                    )
                    started += 1
                except WorkflowAlreadyStartedError:
                    pass
            return started

        started = asyncio.run(start_all())
        return f"{started} reconciliation(s) started for {len(rows)} workspace(s)"

    return ("slack_reconcile", run)


TASK_STALE_AFTER = "10 minutes"


def task_reconcile_job(settings: Settings) -> Job:
    """Fail open tasks whose workflow no longer runs (terminated, timed out, or never started), so
    a thread is never blocked by a task nobody is working on. Tasks waiting for approval have a
    running workflow and are left alone."""

    def run(conn: psycopg.Connection) -> str:
        rows = conn.execute(
            "select task_id, workflow_id from app.maintenance_open_tasks(%s::interval)", (TASK_STALE_AFTER,)
        ).fetchall()
        if not rows:
            return "no open tasks to check"

        async def gone() -> list[tuple[object, str]]:
            from temporalio.client import WorkflowExecutionStatus
            from temporalio.service import RPCError

            from app.temporal import connect_temporal

            client = await connect_temporal(settings, attempts=3, delay=0.5)
            found = []
            for task_id, workflow_id in rows:
                if not workflow_id:
                    found.append((task_id, "the task has no workflow"))
                    continue
                try:
                    described = await client.get_workflow_handle(workflow_id).describe()
                except RPCError:
                    found.append((task_id, "its workflow was not found"))
                    continue
                if described.status != WorkflowExecutionStatus.RUNNING:
                    name = described.status.name.lower() if described.status else "closed"
                    found.append((task_id, f"its workflow ended ({name}) without finishing the task"))
            return found

        failed = 0
        for task_id, reason in asyncio.run(gone()):
            row = conn.execute("select app.maintenance_fail_task(%s, %s)", (task_id, reason)).fetchone()
            failed += 1 if row and row[0] else 0
        return f"checked {len(rows)} open task(s), failed {failed}"

    return ("task_reconcile", run)


def schedule_sync_job(settings: Settings) -> Job:
    """Start a schedule sync for every org with schedules Temporal does not match yet, or pauses
    nobody was told about (whatever the best effort starts after changes missed). One workflow id
    per org and quarter hour, so overlapping cron runs do not duplicate work."""

    def run(conn: psycopg.Connection) -> str:
        orgs = [r[0] for r in conn.execute("select org_id from app.maintenance_schedule_orgs()").fetchall()]
        if not orgs:
            return "all schedules in sync"
        now = datetime.now(UTC)
        slot = f"{now:%Y%m%d%H}{now.minute // 15}"

        async def start_all() -> int:
            from temporalio.exceptions import WorkflowAlreadyStartedError

            from app.temporal import connect_temporal

            client = await connect_temporal(settings, attempts=3, delay=0.5)
            started = 0
            for org_id in orgs:
                try:
                    await client.start_workflow(
                        "ScheduleSyncWorkflow",
                        str(org_id),
                        id=f"schedule-sync-{org_id}-{slot}",
                        task_queue=settings.temporal_task_queue,
                    )
                    started += 1
                except WorkflowAlreadyStartedError:
                    pass
            return started

        started = asyncio.run(start_all())
        return f"{started} schedule sync(s) started for {len(orgs)} org(s)"

    return ("schedule_sync", run)


def sandbox_cleanup_job(settings: Settings) -> Job:
    """Destroy sandboxes whose time ran out (a task that never finished cleanly, a release that
    failed) at the provider, then mark them destroyed. A sandbox the provider no longer has is
    destroyed already."""

    def run(conn: psycopg.Connection) -> str:
        expired = conn.execute(
            "select org_id, id, provider_id from app.maintenance_expired_sandboxes(200)"
        ).fetchall()
        if not expired:
            return "no expired sandboxes"
        if not settings.sandbox_url or settings.sandbox_api_key is None:
            return f"{len(expired)} expired sandbox(es) left: no sandbox provider is configured"

        async def destroy_all() -> int:
            import httpx

            from app.sandbox import HttpSandboxProvider, SandboxError

            url, key = settings.sandbox_url, settings.sandbox_api_key
            assert url is not None
            assert key is not None
            done = 0
            async with httpx.AsyncClient(timeout=20) as http:
                provider = HttpSandboxProvider(url, key.get_secret_value(), http)
                for _org, sandbox_row, provider_id in expired:
                    try:
                        if provider_id:
                            await provider.destroy(str(provider_id))
                    except SandboxError:
                        log.warning(
                            "could not destroy an expired sandbox", extra={"sandbox": str(sandbox_row)}
                        )
                        continue
                    conn.execute("select app.maintenance_sandbox_destroyed(%s)", (sandbox_row,))
                    done += 1
            return done

        done = asyncio.run(destroy_all())
        return f"{done} of {len(expired)} expired sandbox(es) destroyed"

    return ("sandbox_cleanup", run)


def run_jobs(settings: Settings) -> int:
    jobs = [
        *JOBS,
        slack_reconcile_job(settings),
        task_reconcile_job(settings),
        schedule_sync_job(settings),
        sandbox_cleanup_job(settings),
    ]
    results = run_all(settings.component_dsn("maintenance"), jobs)
    for r in results:
        status = "ok" if r.ok else "FAILED"
        print(f"{r.name}: {status} ({r.detail}) in {r.seconds:.2f}s")
    return 0 if all(r.ok for r in results) else 1

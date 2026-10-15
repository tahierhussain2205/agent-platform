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


def run_jobs(settings: Settings) -> int:
    jobs = [*JOBS, slack_reconcile_job(settings), task_reconcile_job(settings)]
    results = run_all(settings.component_dsn("maintenance"), jobs)
    for r in results:
        status = "ok" if r.ok else "FAILED"
        print(f"{r.name}: {status} ({r.detail}) in {r.seconds:.2f}s")
    return 0 if all(r.ok for r in results) else 1

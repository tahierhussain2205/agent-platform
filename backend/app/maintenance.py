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


def run_jobs(settings: Settings) -> int:
    results = run_all(settings.component_dsn("maintenance"), [*JOBS, slack_reconcile_job(settings)])
    for r in results:
        status = "ok" if r.ok else "FAILED"
        print(f"{r.name}: {status} ({r.detail}) in {r.seconds:.2f}s")
    return 0 if all(r.ok for r in results) else 1

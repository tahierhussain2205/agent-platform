"""Maintenance jobs (``app maintenance``), run daily by the Render cron job.

Connects as ``maintenance_service`` (role ``app_maintenance``). Work that crosses tenants goes
through security definer functions, so it does not depend on BYPASSRLS (see migration 001 notes).
Each job is isolated: one failure is reported and the rest still run.
"""

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

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


def run_jobs(settings: Settings) -> int:
    results = run_all(settings.component_dsn("maintenance"))
    for r in results:
        status = "ok" if r.ok else "FAILED"
        print(f"{r.name}: {status} ({r.detail}) in {r.seconds:.2f}s")
    return 0 if all(r.ok for r in results) else 1

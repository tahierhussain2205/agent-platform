"""What follows a member's removal (spec 4.1.6) once the database has applied it: the triggers
already revoked their links, paused their schedules and closed their sessions; this cancels their
running tasks through Temporal, then syncs the org's schedules and tells the admins."""

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from temporalio.client import Client
    from temporalio.service import RPCError, RPCStatusCode

    from app.db.tenant import tenant_tx
    from app.workflows.report_activities import ReportActivities

RETRY = RetryPolicy(maximum_attempts=5, maximum_interval=timedelta(seconds=30))


class MemberActivities:
    def __init__(self, pool: Any, client: Callable[[], Client] = activity.client) -> None:
        self.pool = pool
        self.client = client

    @activity.defn(name="member_cancel_tasks")
    async def cancel_tasks(self, org_id: str, membership_id: str) -> int:
        """Cancel the member's open tasks; each workflow closes its task and status message, and a write
        in progress completes first. Returns how many were asked to cancel."""
        async with tenant_tx(self.pool, org_id) as conn:
            rows = await (
                await conn.execute(
                    "select workflow_id from app.tasks where requested_by = %s and workflow_id is not null "
                    "and status in ('queued', 'running', 'awaiting_approval')",
                    (membership_id,),
                )
            ).fetchall()
        client = self.client()
        for (workflow_id,) in rows:
            try:
                await client.get_workflow_handle(workflow_id).cancel()
            except RPCError as exc:
                if exc.status != RPCStatusCode.NOT_FOUND:
                    raise
        return len(rows)

    def all(self) -> list[Callable[..., Any]]:
        return [self.cancel_tasks]


@workflow.defn(name="MemberRemovedWorkflow")
class MemberRemovedWorkflow:
    @workflow.run
    async def run(self, org_id: str, membership_id: str) -> str:
        timeout = timedelta(seconds=60)
        cancelled = await workflow.execute_activity_method(
            MemberActivities.cancel_tasks,
            args=[org_id, membership_id],
            start_to_close_timeout=timeout,
            retry_policy=RETRY,
        )
        await workflow.execute_activity_method(
            ReportActivities.sync, org_id, start_to_close_timeout=timeout, retry_policy=RETRY
        )
        notified = await workflow.execute_activity_method(
            ReportActivities.notify, org_id, start_to_close_timeout=timeout, retry_policy=RETRY
        )
        return f"cancelled {cancelled} task(s); notified {notified} pause(s)"

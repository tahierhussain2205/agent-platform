"""Scheduled report runs and schedule syncing (spec 6.4).

``ScheduledReportWorkflow`` is what each Temporal Schedule starts. It checks the schedule and its
owner's access, waits for a slot under the org's concurrency limit, computes the report, delivers
it once, and closes its task. Access and budget failures pause the schedule; the run then syncs
Temporal and tells the admins, so a paused schedule stops firing.

``ScheduleSyncWorkflow`` makes an org's Temporal Schedules match its rows and sends pause notices.
It is started after anything that may pause or change schedules, and by the maintenance job.
"""

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey
from temporalio.exceptions import ActivityError
from temporalio.workflow import ActivityCancellationType

with workflow.unsafe.imports_passed_through():
    from app.agent.types import ReportComposed, ReportInput, ReportStart
    from app.workflows.report_activities import ReportActivities

RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2), maximum_interval=timedelta(seconds=30), maximum_attempts=4
)
QUICK = timedelta(seconds=60)
MODEL_STEP = timedelta(minutes=15)
MAX_QUEUE_WAIT = timedelta(hours=1)
SCHEDULED_START = SearchAttributeKey.for_datetime("TemporalScheduledStartTime")


async def _do(method: Any, *args: Any, limit: timedelta = QUICK) -> Any:
    return await workflow.execute_activity_method(
        method,
        args=list(args),
        start_to_close_timeout=limit,
        retry_policy=RETRY,
        cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
    )


@workflow.defn(name="ScheduledReportWorkflow")
class ScheduledReportWorkflow:
    @workflow.run
    async def run(self, inp: ReportInput) -> str:
        info = workflow.info()
        # The nominal time (before jitter): the report window is anchored on it.
        scheduled = info.typed_search_attributes.get(SCHEDULED_START) or info.start_time
        start: ReportStart = await _do(ReportActivities.begin, inp, scheduled.isoformat(), info.workflow_id)
        if start.status != "run" or start.ref is None:
            await self._sync(inp)  # a paused or deleted schedule stops firing
            return start.message
        ref = start.ref
        try:
            waited = timedelta()
            while True:
                slot = await _do(ReportActivities.start, ref)
                if slot.status == "gone":
                    return "gone"
                if slot.status == "started":
                    break
                if slot.status == "refused":
                    return await self._pause(inp, start, "budget", slot.message)
                if waited >= MAX_QUEUE_WAIT:
                    await _do(ReportActivities.finish, ref, "failed", "waited too long for a free slot")
                    return "failed"
                await workflow.sleep(timedelta(seconds=15))
                waited += timedelta(seconds=15)
            composed: ReportComposed = await _do(ReportActivities.compose, ref, start, limit=MODEL_STEP)
            if composed.denied or composed.stopped:
                reason = "access_lost" if composed.denied else "budget"
                return await self._pause(inp, start, reason, composed.denied or composed.stopped or "")
            await _do(ReportActivities.deliver, ref, start, composed)
            await _do(ReportActivities.finish, ref, "completed", None)
            return "delivered"
        except asyncio.CancelledError:
            await _do(ReportActivities.finish, ref, "cancelled", "cancelled")
            raise
        except ActivityError as exc:
            reason = str(exc.cause or exc)[:300]
            workflow.logger.warning("scheduled report failed", extra={"reason": reason})
            await _do(ReportActivities.finish, ref, "failed", reason)
            return "failed"  # the schedule stays active: the next run tries again

    async def _pause(self, inp: ReportInput, start: ReportStart, reason: str, detail: str) -> str:
        assert start.ref is not None
        await _do(ReportActivities.pause, inp, reason, detail)
        await _do(ReportActivities.finish, start.ref, "failed", f"schedule paused: {detail}"[:300])
        await self._sync(inp)
        return "paused"

    async def _sync(self, inp: ReportInput) -> None:
        await _do(ReportActivities.sync, inp.org_id)
        await _do(ReportActivities.notify, inp.org_id)


@workflow.defn(name="ScheduleSyncWorkflow")
class ScheduleSyncWorkflow:
    @workflow.run
    async def run(self, org_id: str) -> str:
        synced = await _do(ReportActivities.sync, org_id)
        notified = await _do(ReportActivities.notify, org_id)
        return f"{synced}; notified {notified}"

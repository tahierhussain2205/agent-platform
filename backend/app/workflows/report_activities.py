"""Activities of scheduled report runs and of schedule syncing (the Temporal mirror and pause
notifications). Thin wrappers: the work is in ``ReportRunner`` and ``app.schedules.temporal``."""

from collections.abc import Callable
from typing import Any

from temporalio import activity

from app.agent.reports import ReportRunner
from app.agent.types import Delivered, ReportComposed, ReportInput, ReportStart, StartResult, TaskRef
from app.schedules.temporal import sync_org
from app.workflows.task_activities import translated


class ReportActivities:
    def __init__(self, runner: ReportRunner) -> None:
        self.runner = runner

    @activity.defn(name="report_begin")
    @translated
    async def begin(self, inp: ReportInput, scheduled_at: str, workflow_id: str) -> ReportStart:
        return await self.runner.begin(inp.org_id, inp.schedule_id, scheduled_at, workflow_id)

    @activity.defn(name="report_start")
    @translated
    async def start(self, ref: TaskRef) -> StartResult:
        return await self.runner.start(ref)

    @activity.defn(name="report_compose")
    @translated
    async def compose(self, ref: TaskRef, start: ReportStart) -> ReportComposed:
        return await self.runner.compose_report(ref, start)

    @activity.defn(name="report_deliver")
    @translated
    async def deliver(self, ref: TaskRef, start: ReportStart, composed: ReportComposed) -> Delivered:
        return await self.runner.deliver_report(ref, start, composed)

    @activity.defn(name="report_finish")
    @translated
    async def finish(self, ref: TaskRef, status: str, error: str | None) -> None:
        await self.runner.finish(ref, status, error)

    @activity.defn(name="report_pause")
    @translated
    async def pause(self, inp: ReportInput, reason: str, detail: str) -> bool:
        return await self.runner.pause(inp.org_id, inp.schedule_id, reason, detail)

    @activity.defn(name="schedules_sync")
    async def sync(self, org_id: str) -> str:
        synced, failed = await sync_org(self.runner.d.pool, activity.client(), self.runner.d.settings, org_id)
        if failed:
            raise RuntimeError(f"{failed} schedule(s) could not be synced with Temporal")  # retried
        return f"synced {synced}"

    @activity.defn(name="schedules_notify")
    @translated
    async def notify(self, org_id: str) -> int:
        return await self.runner.notify_pauses(org_id)

    def all(self) -> list[Callable[..., Any]]:
        return [
            self.begin,
            self.start,
            self.compose,
            self.deliver,
            self.finish,
            self.pause,
            self.sync,
            self.notify,
        ]

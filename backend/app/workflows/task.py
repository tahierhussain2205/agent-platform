"""The task workflow: one agent task from start to finish (spec 6.3, 7.1, 11.2).

It waits for its turn (one running task per thread, the org's concurrency limit), keeps one
status message up to date, runs the steps as activities (each a checkpoint), checks budgets
between steps, waits for approval decisions (signals, re-reading the database so a lost signal
is not a lost decision), and always closes the task row and the status message, including on
cancellation and failure.
"""

import asyncio
import contextlib
from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
from temporalio.workflow import ActivityCancellationType

with workflow.unsafe.imports_passed_through():
    from app.agent.types import (
        ActionOutcome,
        ApprovalState,
        Composed,
        Findings,
        PlannedAction,
        TaskRef,
        Understanding,
    )
    from app.workflows.task_activities import TaskActivities

RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2), maximum_interval=timedelta(seconds=30), maximum_attempts=4
)
QUICK = timedelta(seconds=60)
MODEL_STEP = timedelta(minutes=15)
MAX_QUEUE_WAIT = timedelta(hours=1)
APPROVAL_POLL = timedelta(minutes=15)


class StopTask(Exception):  # noqa: N818 - control flow, not an error
    def __init__(self, reason: str, done: list[str]) -> None:
        super().__init__(reason)
        self.reason, self.done = reason, done


@workflow.defn(name="TaskWorkflow")
class TaskWorkflow:
    def __init__(self) -> None:
        self.decisions: dict[str, str] = {}
        self.status_ts: str | None = None
        self.stage = "queued"

    @workflow.signal(name="approval_decided")
    def approval_decided(self, approval_id: str, decision: str) -> None:
        self.decisions[approval_id] = decision

    @workflow.query(name="stage")
    def current_stage(self) -> str:
        return self.stage

    async def _do(self, method: Any, *args: Any, limit: timedelta = QUICK) -> Any:
        # A cancelled task lets a running activity finish: a write is never interrupted halfway.
        return await workflow.execute_activity_method(
            method,
            args=list(args),
            start_to_close_timeout=limit,
            retry_policy=RETRY,
            cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )

    async def _status(self, ref: TaskRef, text: str, running: bool = True) -> None:
        self.status_ts = await self._do(TaskActivities.status, ref, text, self.status_ts, running)

    @workflow.run
    async def run(self, ref: TaskRef) -> str:
        try:
            return await self._run(ref)
        except asyncio.CancelledError:
            self.stage = "cancelled"
            await self._do(TaskActivities.finish, ref, "cancelled", "cancelled by a person")
            if self.status_ts:
                await self._status(ref, "🛑 Cancelled.", running=False)
            raise
        except StopTask as stop:
            return await self._stopped(ref, stop)
        except ActivityError as exc:
            self.stage = "failed"
            reason = str(exc.cause or exc)[:300]
            workflow.logger.warning("task failed", extra={"reason": reason})
            await self._do(TaskActivities.finish, ref, "failed", reason)
            await self._status(
                ref,
                "⚠️ I couldn't finish this request. The team can find the details in the audit log.",
                running=False,
            )
            return "failed"

    async def _run(self, ref: TaskRef) -> str:
        start = await self._wait_for_turn(ref)
        if start.status != "started":
            return str(start.status)
        mode, tier = start.mode, start.model_tier
        self.stage = "understanding"
        await self._status(ref, "🔎 Understanding the request…")
        u: Understanding = await self._do(TaskActivities.understand, ref, mode, limit=MODEL_STEP)
        if u.stopped:
            raise StopTask(u.stopped, [])
        if u.question:
            await self._do(TaskActivities.reply, ref, u.question, "question")
            await self._do(TaskActivities.finish, ref, "completed", None)
            await self._status(ref, "❓ I asked a question above.", running=False)
            return "asked"
        if u.playbook_key == "schedule_create":  # a proposal the requester confirms with a button
            proposal: str = await self._do(TaskActivities.propose_schedule, ref, u, limit=MODEL_STEP)
            await self._do(TaskActivities.finish, ref, "completed", None)
            final = {"proposed": "📅 Confirm the schedule above.", "asked": "❓ I asked a question above."}
            await self._status(ref, final.get(proposal, "✅ Done"), running=False)
            return proposal
        if u.mode_note:
            await self._do(TaskActivities.reply, ref, u.mode_note, "mode-note")
        done = ["understood the request"]
        await self._check_budget(ref, done)
        channels = ", ".join(f"#{c}" for c in ((u.brief or {}).get("entities") or {}).get("channels") or [])
        self.stage = "gathering"
        await self._status(ref, f"📖 Reading {channels or 'the conversation'}…")
        f: Findings = await self._do(TaskActivities.gather, ref, u, tier, limit=MODEL_STEP)
        if f.stopped:
            raise StopTask(f.stopped, done)
        done.append(f"read {', '.join(f.channels_read) or 'the sources'}")
        await self._check_budget(ref, done)
        actions: list[PlannedAction] = await self._do(TaskActivities.plan, ref, u, f, tier, limit=MODEL_STEP)
        await self._check_budget(ref, done)
        self.stage = "writing"
        await self._status(ref, "✍️ Writing the answer…")
        c: Composed = await self._do(TaskActivities.compose, ref, u, f, tier, limit=MODEL_STEP)
        notes = []
        for n, action in enumerate(actions, 1):
            outcome: ActionOutcome = await self._do(
                TaskActivities.act, ref, u, action, n, c.answer, c.sources
            )
            if outcome.status == "awaiting_approval":
                self.stage = "awaiting_approval"
                await self._status(ref, f"⏸ Waiting for approval: {action.summary}")
                state = await self._await_approval(ref, outcome)
                self.stage = "acting"
                if state.status == "approved":
                    outcome = await self._do(TaskActivities.execute, ref, u, action, n, c.answer)
                else:
                    outcome = ActionOutcome("skipped", f"{action.summary}: not done ({state.status})")
            notes.append(_note(action, outcome))
        self.stage = "delivering"
        await self._do(TaskActivities.deliver, ref, u, c, notes)
        usage = await self._do(TaskActivities.usage, ref)
        await self._do(TaskActivities.finish, ref, "completed", c.stopped)
        self.stage = "completed"
        await self._status(ref, f"✅ Done · {int(usage['tokens']):,} tokens", running=False)
        return "completed"

    async def _wait_for_turn(self, ref: TaskRef) -> Any:
        """The start result: ``started``, ``gone`` (finished or cancelled meanwhile), ``refused``
        (budget spent) or ``failed`` (waited too long)."""
        waited = timedelta()
        told = False
        while True:
            start = await self._do(TaskActivities.start, ref)
            if start.status in ("started", "gone"):
                return start
            if start.status == "refused":
                await self._do(TaskActivities.reply, ref, start.message, "refused")
                await self._do(TaskActivities.finish, ref, "failed", start.message)
                return start
            if not told:
                await self._status(ref, f"⏳ {start.message}")
                told = True
            if waited >= MAX_QUEUE_WAIT:
                await self._do(TaskActivities.finish, ref, "failed", "waited too long for a free slot")
                await self._status(
                    ref, "⚠️ I waited too long for a free slot. Please ask again later.", running=False
                )
                start.status = "failed"
                return start
            pause = timedelta(seconds=3 if start.status == "wait_session" else 10)
            await workflow.sleep(pause)
            waited += pause

    async def _check_budget(self, ref: TaskRef, done: list[str]) -> None:
        check = await self._do(TaskActivities.budget, ref)
        if not check.ok:
            raise StopTask(check.message, done)

    async def _stopped(self, ref: TaskRef, stop: StopTask) -> str:
        """Budgets stop a task between steps: say what was done, in code, without a model call."""
        self.stage = "stopped"
        done = "; ".join(stop.done) if stop.done else "nothing yet"
        await self._do(
            TaskActivities.reply, ref, f"I stopped because {stop.reason}. Done so far: {done}.", "stopped"
        )
        await self._do(TaskActivities.finish, ref, "completed", f"stopped: {stop.reason}")
        await self._status(ref, "⏹ Stopped: budget reached.", running=False)
        return "stopped"

    async def _await_approval(self, ref: TaskRef, outcome: ActionOutcome) -> ApprovalState:
        assert outcome.approval_id is not None
        approval_id = outcome.approval_id
        deadline = (
            datetime.fromisoformat(outcome.expires_at)
            if outcome.expires_at
            else workflow.now() + timedelta(hours=24)
        )
        while True:
            remaining = deadline - workflow.now()
            timeout = max(min(remaining, APPROVAL_POLL), timedelta(seconds=1))
            with contextlib.suppress(TimeoutError):
                await workflow.wait_condition(lambda: approval_id in self.decisions, timeout=timeout)
            state: ApprovalState = await self._do(TaskActivities.resolve_approval, ref, approval_id)
            if state.status != "pending":
                return state
            self.decisions.pop(approval_id, None)  # a signal ahead of its commit: wait again


def _note(action: PlannedAction, outcome: ActionOutcome) -> str:
    icon = {"done": "✅", "denied": "🚫", "failed": "⚠️", "skipped": "⏭"}.get(outcome.status, "•")
    if outcome.status == "done":
        return f"{icon} {action.summary}."
    return f"{icon} {outcome.message}"

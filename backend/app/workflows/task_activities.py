"""Task activities: thin wrappers over the agent runner's steps, with error translation for
Temporal (errors retrying cannot fix fail at once)."""

import functools
from collections.abc import Awaitable, Callable
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.agent.runner import AgentRunner
from app.agent.types import (
    ActionOutcome,
    ApprovalState,
    BudgetCheck,
    CaseFile,
    Composed,
    Delivered,
    Findings,
    PlannedAction,
    StartResult,
    TaskRef,
    Understanding,
)
from app.llm.gateway import LLMRequestError, LLMResponseError
from app.slack.client import SlackError
from app.slack.service import SlackNotConnectedError
from app.workflows.slack_activities import PERMANENT_SLACK_ERRORS


def translated[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except SlackError as exc:
            if exc.error in PERMANENT_SLACK_ERRORS:
                raise ApplicationError(
                    f"slack {exc.method}: {exc.error}", type="SlackPermanentError", non_retryable=True
                ) from exc
            raise
        except SlackNotConnectedError as exc:
            raise ApplicationError(str(exc), type="SlackNotConnected", non_retryable=True) from exc
        except LLMRequestError as exc:
            raise ApplicationError(str(exc), type="LLMRequestError", non_retryable=True) from exc
        except LLMResponseError as exc:  # retried: the model may answer properly next time
            raise ApplicationError(str(exc), type="LLMResponseError") from exc

    return wrapper


class TaskActivities:
    def __init__(self, runner: AgentRunner) -> None:
        self.r = runner

    @activity.defn(name="task_start")
    @translated
    async def start(self, ref: TaskRef) -> StartResult:
        return await self.r.start(ref)

    @activity.defn(name="task_status")
    @translated
    async def status(self, ref: TaskRef, text: str, ts: str | None, running: bool) -> str | None:
        return await self.r.status(ref, text, ts, running)

    @activity.defn(name="task_reply")
    @translated
    async def reply(self, ref: TaskRef, text: str, key: str) -> str | None:
        return await self.r.reply(ref, text, key)

    @activity.defn(name="task_set_status")
    @translated
    async def set_status(self, ref: TaskRef, status: str) -> None:
        await self.r.set_status(ref, status)

    @activity.defn(name="task_finish")
    @translated
    async def finish(self, ref: TaskRef, status: str, error: str | None) -> None:
        await self.r.finish(ref, status, error)

    @activity.defn(name="task_budget")
    @translated
    async def budget(self, ref: TaskRef) -> BudgetCheck:
        return await self.r.budget(ref)

    @activity.defn(name="task_usage")
    @translated
    async def usage(self, ref: TaskRef) -> dict[str, Any]:
        tokens, cost = await self.r.usage(ref)
        return {"tokens": tokens, "cost": cost}

    @activity.defn(name="agent_understand")
    @translated
    async def understand(self, ref: TaskRef, mode: str) -> Understanding:
        return await self.r.understand(ref, mode)

    @activity.defn(name="agent_gather")
    @translated
    async def gather(self, ref: TaskRef, u: Understanding, tier: str) -> Findings:
        return await self.r.gather(ref, u, tier)

    @activity.defn(name="agent_orient")
    @translated
    async def orient(self, ref: TaskRef, u: Understanding) -> CaseFile:
        return await self.r.orient(ref, u)

    @activity.defn(name="agent_investigate")
    @translated
    async def investigate(
        self, ref: TaskRef, u: Understanding, case: CaseFile, kind: str, tier: str
    ) -> Findings:
        return await self.r.investigate(ref, u, case, kind, tier)

    @activity.defn(name="agent_plan_investigation")
    @translated
    async def plan_investigation(
        self, ref: TaskRef, u: Understanding, case: CaseFile, findings: list[Findings], tier: str
    ) -> Composed:
        return await self.r.plan_investigation(ref, u, case, findings, tier)

    @activity.defn(name="agent_plan")
    @translated
    async def plan(self, ref: TaskRef, u: Understanding, f: Findings, tier: str) -> list[PlannedAction]:
        return await self.r.plan(ref, u, f, tier)

    @activity.defn(name="agent_compose")
    @translated
    async def compose(self, ref: TaskRef, u: Understanding, f: Findings, tier: str) -> Composed:
        return await self.r.compose(ref, u, f, tier)

    @activity.defn(name="agent_act")
    @translated
    async def act(
        self,
        ref: TaskRef,
        u: Understanding,
        action: PlannedAction,
        n: int,
        answer: str,
        sources: list[dict[str, Any]],
    ) -> ActionOutcome:
        return await self.r.act(ref, u, action, n, answer, sources)

    @activity.defn(name="agent_resolve_approval")
    @translated
    async def resolve_approval(self, ref: TaskRef, approval_id: str) -> ApprovalState:
        return await self.r.resolve_approval(ref, approval_id)

    @activity.defn(name="agent_execute")
    @translated
    async def execute(
        self, ref: TaskRef, u: Understanding, action: PlannedAction, n: int, answer: str
    ) -> ActionOutcome:
        return await self.r.execute(ref, u, action, n, answer)

    @activity.defn(name="agent_deliver")
    @translated
    async def deliver(self, ref: TaskRef, u: Understanding, c: Composed, notes: list[str]) -> Delivered:
        return await self.r.deliver(ref, u, c, notes)

    @activity.defn(name="agent_propose_schedule")
    @translated
    async def propose_schedule(self, ref: TaskRef, u: Understanding) -> str:
        from app.agent.schedule_setup import propose_schedule

        return await propose_schedule(self.r, ref, u)

    def all(self) -> list[Callable[..., Any]]:
        return [
            self.start, self.status, self.reply, self.set_status, self.finish, self.budget, self.usage,
            self.understand, self.gather, self.plan, self.compose, self.act, self.resolve_approval,
            self.execute, self.deliver, self.propose_schedule, self.orient, self.investigate,
            self.plan_investigation,
        ]  # fmt: skip

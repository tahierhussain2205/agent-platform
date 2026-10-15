"""Agent tasks end to end: a real Temporal dev server and worker, the Slack and Anthropic stand-ins,
and the API for button clicks (approvals, cancellation)."""

import asyncio
import json
import os
import socket
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
import pytest_asyncio
from temporalio.client import Client, WorkflowFailureError
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from app.agent.runner import AgentDeps, AgentRunner
from app.settings import Settings
from app.slack import signature
from app.temporal import connect_temporal
from app.worker import build_worker
from app.workflows import task as task_module
from app.workflows.slack import AgentRequestWorkflow
from app.workflows.slack_activities import SlackActivities, SlackDeps
from app.workflows.slack_types import SlackRequest
from app.workflows.task import TaskWorkflow
from app.workflows.task_activities import TaskActivities
from tests.agent.conftest import Agent, channel_messages, ui_post
from tests.stack import FAKES, Stack, app_for

TEMPORAL = os.environ.get("TEST_TEMPORAL_ADDRESS", "localhost:7233")
SIGNING_SECRET = "local-slack-signing-secret"


def temporal_up() -> bool:
    host, _, port = TEMPORAL.rpartition(":")
    try:
        socket.create_connection((host, int(port)), timeout=1).close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not temporal_up(), reason="Temporal not reachable (run `make infra`)")


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SIGNING_SECRET)


@dataclass
class Live:
    agent: Agent
    client: Client
    settings: Settings
    api: httpx.AsyncClient

    @property
    def world(self) -> Any:
        return self.agent.world

    async def request(
        self, text: str, user: str = "U0BOB", channel: str = "C0GENERAL", thread_ts: str | None = None
    ) -> tuple[str, str]:
        """Post a message (no bot mention: the dev stack would answer too) and start the intake
        the way ingress does. Returns (event id, message ts)."""
        message = await ui_post(user, channel, text, thread_ts)
        workspace = self.agent.sql("select id from app.workspaces where external_id = 'T0ACME'")[0][0]
        event_id = f"Ev{uuid.uuid4().hex[:12]}"
        req = SlackRequest(
            str(self.world.acme.id), str(workspace), "T0ACME", channel, "channel", user, message["ts"], thread_ts,
            event_id, "app_mention",
        )  # fmt: skip
        result = await self.client.execute_workflow(
            AgentRequestWorkflow.run,
            req,
            id=f"slack-{event_id}",
            task_queue=self.settings.temporal_task_queue,
        )
        assert result == "task_started"
        return event_id, message["ts"]

    async def result(self, event_id: str) -> Any:
        return await asyncio.wait_for(self.client.get_workflow_handle(f"task-{event_id}").result(), 60)

    def task_row(self, event_id: str) -> Any:
        rows = self.agent.sql(
            "select id, status, error, finished_at is not null from app.tasks where workflow_id = %s",
            (f"task-{event_id}",),
        )
        return rows[0] if rows else None

    async def click(
        self,
        user: str,
        action_id: str,
        value: str,
        channel: str = "C0GENERAL",
        ts: str = "",
        team: str = "T0ACME",
    ) -> httpx.Response:
        payload = {
            "type": "block_actions",
            "team": {"id": team},
            "user": {"id": user},
            "container": {"type": "message", "channel_id": channel, "message_ts": ts},
            "channel": {"id": channel},
            "actions": [{"action_id": action_id, "value": value, "action_ts": str(time.time())}],
        }
        body = urlencode({"payload": json.dumps(payload)}).encode()
        stamp = int(time.time())
        return await self.api.post(
            "/slack/interactions",
            content=body,
            headers={
                "x-slack-request-timestamp": str(stamp),
                "x-slack-signature": signature.sign(SIGNING_SECRET, stamp, body),
                "content-type": "application/x-www-form-urlencoded",
            },
        )

    async def approval(self, event_id: str) -> tuple[str, str]:
        """Wait for the task's approval request: (approval id, message ts)."""

        def find() -> Any:
            rows = self.agent.sql(
                "select a.id, a.message_ref from app.approvals a join app.tasks t on t.id = a.task_id "
                "where t.workflow_id = %s and a.message_ref is not null",
                (f"task-{event_id}",),
            )
            return rows[0] if rows else None

        row = await wait_for(find)
        return str(row[0]), str(row[1]).rsplit(":", 1)[1]


async def wait_for(check: Callable[[], Any | Awaitable[Any]], seconds: float = 40) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(0.3)
    raise AssertionError("condition not met in time")


def runner_for(agent: Agent) -> AgentRunner:
    deps = AgentDeps(**{**agent.runner.d.__dict__, "sink_factory": None})  # posts to the Slack stand-in
    return AgentRunner(deps)


def activities_for(agent: Agent, stack: Stack) -> SlackActivities:
    deps = SlackDeps(
        stack.pool, stack.vault, stack.slack, stack.kv, "http://console.test", agent_enabled=True
    )
    return SlackActivities(deps, TaskActivities(runner_for(agent)))


@pytest_asyncio.fixture(loop_scope="session")
async def live(agent: Agent, stack: Stack) -> AsyncIterator[Live]:
    settings = Settings(temporal_address=TEMPORAL, temporal_task_queue=f"agent-test-{uuid.uuid4().hex[:8]}")
    client = await connect_temporal(settings)
    app = app_for(stack, temporal=client)
    async with (
        build_worker(client, settings, activities_for(agent, stack)),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://api"
        ) as api,
    ):
        yield Live(agent, client, settings, api)
    agent.sql(
        "update app.tasks set status = 'completed', finished_at = now() where org_id = %s "
        "and status in ('queued', 'running', 'awaiting_approval')",
        (agent.world.acme.id,),
    )


def thread_of(messages: list[dict[str, Any]], ts: str) -> list[dict[str, Any]]:
    return [m for m in messages if m.get("thread_ts") == ts and m.get("bot_id")]


# ------------------------------------------------------------------ end to end


async def test_a_question_is_answered_in_the_thread_with_sources(live: Live) -> None:
    t = uuid.uuid4().hex[:6]
    await ui_post("U0QUINN", "C0BUGS", f"Search returns stale prices {t}")
    event_id, ts = await live.request("summarize today's discussion in #bugs")
    assert await live.result(event_id) == "completed"
    thread = thread_of(await channel_messages("C0GENERAL"), ts)
    status = [m for m in thread if m.get("edited")]
    assert status[0]["text"].startswith("✅ Done · ")
    answer = [m for m in thread if "Here's what I found" in m["text"]]
    assert len(answer) == 1
    assert f"Search returns stale prices {t}" in answer[0]["text"]
    assert "*Sources*" in json.dumps(answer[0]["blocks"])
    task = live.task_row(event_id)
    assert (task[1], task[2], task[3]) == ("completed", None, True)


async def test_a_non_member_gets_a_plain_refusal(live: Live) -> None:
    event_id, ts = await live.request("summarize #leads-private")
    assert await live.result(event_id) == "completed"
    answer = [m for m in thread_of(await channel_messages("C0GENERAL"), ts) if "not a member" in m["text"]]
    assert answer
    assert "reorg" not in json.dumps(answer)


async def test_tasks_in_one_thread_run_one_at_a_time(live: Live) -> None:
    first_event, ts = await live.request("summarize today's discussion in #bugs")
    second_event, _ = await live.request("and in #general?", thread_ts=ts)
    assert await live.result(first_event) == "completed"
    assert await live.result(second_event) == "completed"
    rows = live.agent.sql(
        "select workflow_id, started_at, finished_at from app.tasks where workflow_id in (%s, %s)",
        (f"task-{first_event}", f"task-{second_event}"),
    )
    times = {w: (s, f) for w, s, f in rows}
    assert times[f"task-{second_event}"][0] >= times[f"task-{first_event}"][1]


# ------------------------------------------------------------------ approvals and cancellation


async def test_sharing_is_approved_from_slack_and_posted(live: Live) -> None:
    t = uuid.uuid4().hex[:6]
    await ui_post("U0QUINN", "C0BUGS", f"Cart totals wrong {t}")
    event_id, _ = await live.request("summarize today's discussion in #bugs and share it in #general")
    approval_id, message_ts = await live.approval(event_id)
    # Someone else (not the requester, not an admin) cannot decide.
    assert (await live.click("U0QUINN", "approval_approve", approval_id, ts=message_ts)).status_code == 200
    refused = await wait_for(lambda: _ephemeral("U0QUINN", "Only the person who asked or an admin"))
    assert refused
    assert live.agent.sql("select status from app.approvals where id = %s", (approval_id,)) == [("pending",)]
    assert (await live.click("U0BOB", "approval_approve", approval_id, ts=message_ts)).status_code == 200
    assert await live.result(event_id) == "completed"
    shared = [
        m
        for m in await channel_messages("C0GENERAL")
        if not m.get("thread_ts") and f"Cart totals wrong {t}" in m.get("text", "")
    ]
    assert len(shared) == 1
    decided = [m for m in await channel_messages("C0GENERAL") if m.get("ts") == message_ts]
    assert decided[0]["text"] == "✅ Approved by <@U0BOB>"
    # Decisions are final.
    await live.click("U0ALICE", "approval_reject", approval_id, ts=message_ts)
    assert live.agent.sql("select status, decided_by from app.approvals where id = %s", (approval_id,)) == [
        ("approved", live.world.acme.user)
    ]
    audited = live.agent.sql(
        "select action, actor_id from app.audit_log where action like 'approval.%%' and task_id = %s",
        (live.task_row(event_id)[0],),
    )
    assert audited == [("approval.approved", live.world.acme.user)]


async def test_a_rejected_share_does_nothing(live: Live) -> None:
    t = uuid.uuid4().hex[:6]
    await ui_post("U0QUINN", "C0BUGS", f"Rejected share {t}")
    event_id, ts = await live.request("summarize today's discussion in #bugs and share it in #general")
    approval_id, message_ts = await live.approval(event_id)
    await live.click("U0ALICE", "approval_reject", approval_id, ts=message_ts)  # an admin may decide too
    assert await live.result(event_id) == "completed"
    top_level = [m for m in await channel_messages("C0GENERAL") if not m.get("thread_ts")]
    assert not [m for m in top_level if f"Rejected share {t}" in m.get("text", "")]
    answer = [
        m for m in thread_of(await channel_messages("C0GENERAL"), ts) if "Here's what I found" in m["text"]
    ]
    assert "not done (rejected)" in json.dumps(answer)


async def test_cancelling_a_waiting_task(live: Live) -> None:
    event_id, ts = await live.request("summarize #bugs and share it in #general")
    approval_id, _ = await live.approval(event_id)
    task_id = str(live.task_row(event_id)[0])
    await live.click("U0QUINN", "task_cancel", task_id)
    await wait_for(lambda: _ephemeral("U0QUINN", "Only the person who asked or an admin can cancel"))
    await live.click("U0BOB", "task_cancel", task_id)
    with pytest.raises(WorkflowFailureError):
        await live.result(event_id)
    assert live.task_row(event_id)[1] == "cancelled"
    assert live.agent.sql("select status from app.approvals where id = %s", (approval_id,)) == [("expired",)]
    status = [m for m in thread_of(await channel_messages("C0GENERAL"), ts) if m.get("edited")]
    assert status[0]["text"] == "🛑 Cancelled."
    assert (
        await live.click("U0BOB", "task_cancel", task_id)
    ).status_code == 200  # already cancelled: told, no error


async def test_another_orgs_approval_cannot_be_decided(live: Live) -> None:
    """An approval id from Acme clicked in Globex's workspace is invisible there."""
    event_id, _ = await live.request("summarize #bugs and share it in #general")
    approval_id, _ = await live.approval(event_id)
    await seed_globex(live)
    payload_user = "U0DAVE"
    response = await live.click(
        payload_user, "approval_approve", approval_id, channel="C0GXINCIDENT", team="T0GLOBEX"
    )
    assert response.status_code == 200
    assert live.agent.sql("select status from app.approvals where id = %s", (approval_id,)) == [("pending",)]


async def seed_globex(live: Live) -> None:
    from tests.stack import seed_slack_token

    await seed_slack_token(live.agent.stack, live.world.globex.id, "T0GLOBEX")


# ------------------------------------------------------------------ budgets and failures


async def test_budget_stops_the_task_between_steps(live: Live) -> None:
    acme = live.world.acme
    used = live.agent.sql(
        "select coalesce(sum(tokens), 0) from app.usage_daily where membership_id = %s "
        "and day = (now() at time zone 'UTC')::date",
        (acme.user,),
    )[0][0]
    live.agent.sql(
        "update app.org_settings set user_daily_token_limit = %s where org_id = %s", (used + 50, acme.id)
    )
    try:
        event_id, ts = await live.request("summarize today's discussion in #bugs")
        assert await live.result(event_id) == "stopped"
    finally:
        live.agent.sql(
            "update app.org_settings set user_daily_token_limit = null where org_id = %s", (acme.id,)
        )
    notice = [
        m
        for m in thread_of(await channel_messages("C0GENERAL"), ts)
        if m["text"].startswith("I stopped because")
    ]
    assert "daily token limit" in notice[0]["text"]
    assert "Done so far: nothing yet" in notice[0]["text"]  # the limit was reached inside the first step
    task = live.task_row(event_id)
    assert task[1] == "completed"
    assert task[2].startswith("stopped: your daily token limit")


async def test_a_task_is_refused_when_the_budget_is_already_spent(live: Live) -> None:
    acme = live.world.acme
    live.agent.sql("update app.org_settings set user_daily_token_limit = 1 where org_id = %s", (acme.id,))
    try:
        event_id, ts = await live.request("summarize today's discussion in #bugs")
        assert await live.result(event_id) == "refused"
    finally:
        live.agent.sql(
            "update app.org_settings set user_daily_token_limit = null where org_id = %s", (acme.id,)
        )
    refusal = [
        m for m in thread_of(await channel_messages("C0GENERAL"), ts) if m["text"].startswith("I can't start")
    ]
    assert refusal
    assert live.task_row(event_id)[1] == "failed"


async def test_a_model_that_keeps_refusing_fails_the_task_cleanly(live: Live) -> None:
    async with httpx.AsyncClient() as client:
        await client.post(f"{FAKES}/_admin/anthropic/faults", json={"status": 400, "count": 1})
    event_id, ts = await live.request("summarize today's discussion in #bugs")
    assert await live.result(event_id) == "failed"
    status = [m for m in thread_of(await channel_messages("C0GENERAL"), ts) if m.get("edited")]
    assert status[0]["text"].startswith("⚠️ I couldn't finish")
    assert live.task_row(event_id)[1] == "failed"


async def test_a_lost_approval_signal_is_recovered(
    agent: Agent, stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision is in the database but the signal never arrives: the waiting task re-reads
    the database on its next poll. (Unsandboxed worker, so the shorter poll applies.)"""
    monkeypatch.setattr(task_module, "APPROVAL_POLL", timedelta(seconds=1))
    settings = Settings(
        temporal_address=TEMPORAL, temporal_task_queue=f"agent-unsandboxed-{uuid.uuid4().hex[:8]}"
    )
    client = await connect_temporal(settings)
    slack = activities_for(agent, stack)
    assert slack.tasks is not None
    worker = Worker(
        client, task_queue=settings.temporal_task_queue, workflows=[AgentRequestWorkflow, TaskWorkflow],
        activities=[slack.identify_requester, slack.check_channel, slack.create_task, *slack.tasks.all()],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )  # fmt: skip
    async with worker:
        live = Live(agent, client, settings, httpx.AsyncClient())
        event_id, _ = await live.request("summarize #bugs and share it in #general")
        approval_id, _ = await live.approval(event_id)
        agent.sql(
            "update app.approvals set status = 'rejected', decided_by = %s where id = %s",
            (agent.world.acme.user, approval_id),
        )
        assert await live.result(event_id) == "completed"


def _ephemeral(user: str, text: str) -> Any:
    response = httpx.get(f"{FAKES}/_admin/slack/ephemeral", params={"team": "T0ACME", "user": user})
    return [e for e in response.json() if text in e.get("text", "")]

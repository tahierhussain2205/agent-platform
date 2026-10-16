"""Agent tasks on a real Temporal dev server and worker: the ``Live`` harness behind the ``live``
fixture (tests/agent/conftest.py), with the API for button clicks (approvals, cancellation)."""

import asyncio
import json
import os
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from temporalio.client import Client

from app.agent.runner import AgentDeps, AgentRunner
from app.settings import Settings
from app.slack import signature
from app.workflows.slack import AgentRequestWorkflow
from app.workflows.slack_activities import SlackActivities, SlackDeps
from app.workflows.slack_types import SlackRequest
from app.workflows.task_activities import TaskActivities
from tests.agent.conftest import Agent, ui_post
from tests.stack import Stack

TEMPORAL = os.environ.get("TEST_TEMPORAL_ADDRESS", "localhost:7233")
SIGNING_SECRET = "local-slack-signing-secret"


def temporal_up() -> bool:
    host, _, port = TEMPORAL.rpartition(":")
    try:
        socket.create_connection((host, int(port)), timeout=1).close()
        return True
    except OSError:
        return False


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


def thread_of(messages: list[dict[str, Any]], ts: str) -> list[dict[str, Any]]:
    return [m for m in messages if m.get("thread_ts") == ts and m.get("bot_id")]

"""Slack workflows end to end through a real Temporal dev server, a real worker and the Slack
stand-in: activity registration, data conversion and the decision paths."""

import os
import socket
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import psycopg
import pytest
import pytest_asyncio
from temporalio.client import Client

from app.settings import Settings
from app.temporal import connect_temporal
from app.worker import build_worker
from app.workflows.slack_activities import SlackActivities, SlackDeps
from app.workflows.slack_types import SlackRequest, SyncRequest
from tests.slack.conftest import FAKES, Stack, fakes_up, seed_slack_token

TEMPORAL = os.environ.get("TEST_TEMPORAL_ADDRESS", "localhost:7233")


def temporal_up() -> bool:
    host, _, port = TEMPORAL.rpartition(":")
    try:
        socket.create_connection((host, int(port)), timeout=1).close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not (fakes_up() and temporal_up()), reason="Temporal or the Slack stand-in not reachable"
)


@pytest_asyncio.fixture(loop_scope="session")
async def running(stack: Stack, env: Any) -> AsyncIterator[tuple[Client, Settings]]:
    await seed_slack_token(stack, env.world.acme.id, "T0ACME")
    settings = Settings(temporal_address=TEMPORAL, temporal_task_queue=f"slack-test-{uuid.uuid4().hex[:8]}")
    client = await connect_temporal(settings)
    activities = SlackActivities(
        SlackDeps(
            pool=stack.pool,
            vault=stack.vault,
            slack=stack.slack,
            kv=stack.kv,
            console_url="http://console.test",
        )
    )
    async with build_worker(client, settings, activities):
        yield client, settings


async def post(user: str, channel: str, text: str) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{FAKES}/slack/_ui/post",
            json={"team": "T0ACME", "user": user, "channel": channel, "text": text},
        )
    message: dict[str, Any] = response.json()["message"]
    return message


async def messages(channel: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{FAKES}/_admin/slack/messages", params={"team": "T0ACME", "channel": channel}
        )
    listed: list[dict[str, Any]] = response.json()
    return listed


def request(env: Any, user: str, channel: str, ts: str, channel_type: str = "channel") -> SlackRequest:
    with psycopg.connect(env.world.url) as conn:
        row = conn.execute("select id from app.workspaces where external_id = 'T0ACME'").fetchone()
    assert row is not None
    return SlackRequest(
        str(env.world.acme.id),
        str(row[0]),
        "T0ACME",
        channel,
        channel_type,
        user,
        ts,
        None,
        f"Ev{uuid.uuid4().hex[:10]}",
        "app_mention",
    )


async def bot_leaves(channel: str) -> None:
    """The stand-in is shared with the dev stack and the e2e suite (which adds the bot to #random):
    tests that need the bot outside a channel say so."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{FAKES}/slack/_ui/join",
            json={"team": "T0ACME", "channel": channel, "user": "U0ACMEBOT", "join": False},
        )
    response.raise_for_status()


async def run(client: Client, settings: Settings, workflow: str, arg: Any) -> Any:
    return await client.execute_workflow(
        workflow, arg, id=f"test-{uuid.uuid4().hex}", task_queue=settings.temporal_task_queue
    )


async def test_mention_in_an_allowlisted_channel_gets_a_threaded_reply(
    running: tuple[Client, Settings], env: Any
) -> None:
    client, settings = running
    # No mention in the text: the running Compose stack would otherwise answer the same message.
    message = await post("U0BOB", "C0BUGS", "what broke today?")
    assert (
        await run(client, settings, "AgentRequestWorkflow", request(env, "U0BOB", "C0BUGS", message["ts"]))
        == "replied"
    )
    replies = [m for m in await messages("C0BUGS") if m.get("thread_ts") == message["ts"] and m.get("bot_id")]
    assert len(replies) == 1


async def test_outsider_gets_the_access_request_flow(running: tuple[Client, Settings], env: Any) -> None:
    client, settings = running
    with psycopg.connect(env.world.url) as conn:
        conn.execute(
            "update app.access_requests set status = 'dismissed', resolved_at = now() where external_user_id = 'U0MALLORY' and status = 'pending'"
        )
        conn.commit()
    message = await post("U0MALLORY", "C0GENERAL", "hi")
    assert (
        await run(
            client, settings, "AgentRequestWorkflow", request(env, "U0MALLORY", "C0GENERAL", message["ts"])
        )
        == "access_requested"
    )


async def test_mention_in_a_channel_that_is_not_allowlisted(
    running: tuple[Client, Settings], env: Any
) -> None:
    client, settings = running
    with psycopg.connect(env.world.url) as conn:
        conn.execute("update app.channels set is_allowlisted = false where external_id = 'C0RANDOM'")
        conn.commit()
    # The bot is not even in #random, so the private notice falls back to a DM.
    await bot_leaves("C0RANDOM")
    assert (
        await run(client, settings, "AgentRequestWorkflow", request(env, "U0BOB", "C0RANDOM", "1.1"))
        == "channel_not_allowlisted"
    )
    assert any("not enabled in this channel" in m["text"] for m in await messages("U0BOB"))


async def test_permanent_slack_errors_are_not_retried(running: tuple[Client, Settings], env: Any) -> None:
    """Allowlisted channel, the requester is a member, but the bot is not: the reply fails with
    not_in_channel. That cannot get better by retrying, so the workflow fails at once."""
    import time

    from temporalio.client import WorkflowFailureError

    client, settings = running
    with psycopg.connect(env.world.url) as conn:
        conn.execute(
            "update app.channels set is_allowlisted = true, archived = false where external_id = 'C0RANDOM'"
        )
        conn.commit()
    await bot_leaves("C0RANDOM")
    started = time.monotonic()
    with pytest.raises(WorkflowFailureError) as info:
        await run(client, settings, "AgentRequestWorkflow", request(env, "U0BOB", "C0RANDOM", "1.1"))
    assert time.monotonic() - started < 5  # retries with backoff would take far longer
    cause = info.value.cause
    assert "SlackPermanentError" in repr(cause) or "not_in_channel" in repr(getattr(cause, "cause", ""))
    with psycopg.connect(env.world.url) as conn:
        conn.execute("update app.channels set is_allowlisted = false where external_id = 'C0RANDOM'")
        conn.commit()


async def test_sync_workflow_returns_counts(running: tuple[Client, Settings], env: Any) -> None:
    client, settings = running
    req = request(env, "U0BOB", "C0BUGS", "1.1")
    result = await run(
        client, settings, "SyncWorkspaceWorkflow", SyncRequest(req.org_id, req.workspace_id, "T0ACME")
    )
    assert result["channels"] >= 4

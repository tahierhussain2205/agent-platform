"""Agent test fixtures: the runner wired to the module's world database, the Slack stand-in and the
Anthropic stand-in, with a capturing sink (nothing is posted unless a test asks for Slack)."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import psycopg
import pytest
import pytest_asyncio

from app.agent.outbox import CaptureSink, Sink
from app.agent.runner import AgentDeps, AgentRunner
from app.agent.types import TaskRef
from app.llm.gateway import LLMGateway, Route
from app.workflows.slack_activities import SlackActivities, SlackDeps
from app.workflows.slack_types import Requester, SlackRequest
from tests.stack import FAKES, PROVIDER_SETTINGS, Env, Stack, fakes_up, seed_slack_token

if TYPE_CHECKING:
    from tests.agent.live import Live

MODELS = {"fast": "claude-haiku-4-5-20251001", "standard": "claude-sonnet-5", "advanced": "claude-opus-5-5"}

pytestmark = pytest.mark.skipif(not fakes_up(), reason="stand-ins not reachable (run `make infra`)")


@pytest.fixture(scope="module")
def acme_slack(env: Env) -> None:
    """Acme's Slack integration enabled; the bot token is stored by ``agent`` (needs the vault)."""
    with psycopg.connect(env.world.url) as conn:
        conn.execute(
            "update app.org_settings set user_daily_token_limit = null, org_monthly_token_limit = null, "
            "max_concurrent_tasks = 10 where org_id = %s",
            (env.world.acme.id,),
        )
        conn.commit()


@dataclass
class Agent:
    stack: Stack
    runner: AgentRunner
    sink: CaptureSink
    slack_activities: SlackActivities
    gateway: LLMGateway

    @property
    def world(self) -> Any:
        return self.stack.env.world

    def sql(self, statement: str, params: tuple[Any, ...] = ()) -> Any:
        with psycopg.connect(self.world.url) as conn:
            cur = conn.execute(statement, params)
            rows = cur.fetchall() if cur.description else None
            conn.commit()
            return rows

    async def task(
        self, user: str = "U0BOB", channel: str = "C0GENERAL", ts: str | None = None, thread_ts: str | None = None,
        channel_type: str = "channel",
    ) -> TaskRef:  # fmt: skip
        """A task created through the intake activity, as a mention would."""
        workspace = self.sql("select id from app.workspaces where external_id = 'T0ACME'")[0][0]
        member = self.sql(
            "select pi.membership_id, u.email from app.platform_identities pi join app.org_memberships m "
            "on m.id = pi.membership_id join app.users u on u.id = m.user_id "
            "where pi.workspace_id = %s and pi.external_user_id = %s",
            (workspace, user),
        )[0]
        req = SlackRequest(
            str(self.world.acme.id), str(workspace), "T0ACME", channel, channel_type, user,
            ts or f"{1_700_000_000 + uuid.uuid4().int % 10**8}.000100", thread_ts, f"Ev{uuid.uuid4().hex[:12]}",
            "app_mention",
        )  # fmt: skip
        ref = await self.slack_activities.create_task(req, Requester(str(member[0]), member[1], "active"))
        assert ref is not None
        return ref

    def with_slack(self) -> AgentRunner:
        """The same runner, posting to the Slack stand-in for real."""
        deps = AgentDeps(**{**self.runner.d.__dict__, "sink_factory": None})
        return AgentRunner(deps)


@pytest_asyncio.fixture(loop_scope="session")
async def agent(stack: Stack, acme_slack: None) -> AsyncIterator[Agent]:
    await seed_slack_token(stack, stack.env.world.acme.id, "T0ACME")
    gateway = LLMGateway(
        [Route(f"{FAKES}/anthropic", "sk-ant-local-key")], stack.pool, stack.kv, MODELS,
        rpm={"interactive": 10_000, "batch": 10_000},
    )  # fmt: skip
    sink = CaptureSink()

    def capture(team: str, token: str) -> Sink:
        return sink

    http = httpx.AsyncClient(timeout=30.0)
    deps = AgentDeps(
        stack.pool,
        stack.vault,
        stack.slack,
        stack.kv,
        gateway,
        "http://console.test",
        24,
        capture,
        settings=PROVIDER_SETTINGS,
        http=http,
    )
    slack_activities = SlackActivities(
        SlackDeps(stack.pool, stack.vault, stack.slack, stack.kv, "http://console.test", agent_enabled=True)
    )
    yield Agent(stack, AgentRunner(deps), sink, slack_activities, gateway)
    await gateway.aclose()
    await http.aclose()


async def ui_post(user: str, channel: str, text: str, thread_ts: str | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{FAKES}/slack/_ui/post",
            json={"team": "T0ACME", "user": user, "channel": channel, "text": text, "thread_ts": thread_ts},
        )
    message: dict[str, Any] = r.json()["message"]
    return message


async def model_requests() -> list[dict[str, Any]]:
    async with httpx.AsyncClient() as client:
        data: list[dict[str, Any]] = (await client.get(f"{FAKES}/_admin/anthropic/requests")).json()
    return data


async def script_model(*responses: list[dict[str, Any]]) -> None:
    async with httpx.AsyncClient() as client:
        await client.post(f"{FAKES}/_admin/anthropic/script", json={"responses": list(responses)})


async def reset_model() -> None:
    async with httpx.AsyncClient() as client:
        await client.post(f"{FAKES}/_admin/anthropic/reset")


async def channel_messages(channel: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient() as client:
        data: list[dict[str, Any]] = (
            await client.get(f"{FAKES}/_admin/slack/messages", params={"team": "T0ACME", "channel": channel})
        ).json()
    return data


async def bot_joins(channel: str, join: bool = True) -> None:
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{FAKES}/slack/_ui/join",
            json={"team": "T0ACME", "channel": channel, "user": "U0ACMEBOT", "join": join},
        )


@pytest_asyncio.fixture(loop_scope="session")
async def live(agent: Agent, stack: Stack, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator["Live"]:
    """A Temporal worker on a queue of its own and the API, for tasks end to end (skips without
    Temporal)."""
    from app.settings import Settings
    from app.temporal import connect_temporal
    from app.worker import build_worker
    from tests.agent.live import SIGNING_SECRET, TEMPORAL, Live, activities_for, temporal_up
    from tests.stack import app_for

    if not temporal_up():
        pytest.skip("Temporal not reachable (run `make infra`)")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SIGNING_SECRET)
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

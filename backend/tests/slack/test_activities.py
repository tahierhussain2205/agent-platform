"""Slack activities against the local Slack stand-in (fakes service) and a private world."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import psycopg
import pytest
import pytest_asyncio

from app.workflows.slack_activities import SlackActivities, SlackDeps
from app.workflows.slack_types import HomeRequest, Requester, SlackRequest, SyncRequest
from tests.slack.conftest import FAKES, Stack, fakes_up, seed_slack_token
from tests.world import World

pytestmark = pytest.mark.skipif(not fakes_up(), reason="Slack stand-in not reachable (run `make infra`)")


@pytest_asyncio.fixture(loop_scope="session")
async def acts(stack: Stack, env: Any) -> AsyncIterator[SlackActivities]:
    await seed_slack_token(stack, env.world.acme.id, "T0ACME")
    yield SlackActivities(
        SlackDeps(
            pool=stack.pool,
            vault=stack.vault,
            slack=stack.slack,
            kv=stack.kv,
            console_url="http://console.test",
        )
    )


def req(
    world: World,
    user: str,
    channel: str = "C0BUGS",
    channel_type: str = "channel",
    ts: str = "1700000000.000100",
    event_id: str = "EvTEST",
    thread_ts: str | None = None,
) -> SlackRequest:
    workspace = q(world, "select id from app.workspaces where external_id = 'T0ACME'")
    return SlackRequest(
        str(world.acme.id),
        str(workspace),
        "T0ACME",
        channel,
        channel_type,
        user,
        ts,
        thread_ts,
        event_id,
        "app_mention",
    )


def q(world: World, query: str, params: tuple[Any, ...] = ()) -> Any:
    with psycopg.connect(world.url) as conn:
        row = conn.execute(query, params).fetchone()
        return row[0] if row and len(row) == 1 else row


async def fakes_get(path: str, **params: str) -> Any:
    async with httpx.AsyncClient() as client:
        return (await client.get(f"{FAKES}{path}", params=params)).json()


# ------------------------------------------------------------------ requester identification


async def test_known_slack_identity(acts: SlackActivities, env: Any) -> None:
    who = await acts.identify_requester(req(env.world, "U0BOB"))
    assert (who.membership_id, who.email, who.status) == (str(env.world.acme.user), "bob@acme.test", "active")


async def test_invited_person_is_matched_by_email_and_linked(acts: SlackActivities, env: Any) -> None:
    world = env.world
    who = await acts.identify_requester(req(world, "U0CAROL"))
    assert (who.email, who.status) == ("carol@acme.test", "invited")
    assert who.membership_id is not None
    assert (
        q(world, "select matched_email from app.platform_identities where external_user_id = 'U0CAROL'")
        == "carol@acme.test"
    )


async def test_people_who_are_not_org_users_are_unknown(acts: SlackActivities, env: Any) -> None:
    who = await acts.identify_requester(req(env.world, "U0MALLORY"))
    assert (who.membership_id, who.email) == (None, "mallory@acme.test")
    assert (await acts.identify_requester(req(env.world, "U0ACMEBOT"))).membership_id is None  # bots never


async def test_removed_members_are_not_requesters(acts: SlackActivities, env: Any) -> None:
    world = env.world
    with psycopg.connect(world.url) as conn:
        conn.execute(
            "update app.org_memberships set status = 'removed' where id = (select m.id from app.org_memberships m join app.users u on u.id = m.user_id where u.email = 'frank@both.test' and m.org_id = %s)",
            (world.acme.id,),
        )
        conn.commit()
    assert (await acts.identify_requester(req(world, "U0FRANKA"))).membership_id is None


# ------------------------------------------------------------------ access requests


async def test_access_request_is_opened_once_and_admins_are_asked(acts: SlackActivities, env: Any) -> None:
    world = env.world
    request = req(world, "U0MALLORY", channel="C0GENERAL")
    assert await acts.request_access(request, Requester(None, "mallory@acme.test")) is True
    assert (
        await acts.request_access(request, Requester(None, "mallory@acme.test")) is False
    )  # still pending: no new DMs
    assert (
        q(
            world,
            "select count(*) from app.access_requests where external_user_id = 'U0MALLORY' and status = 'pending'",
        )
        == 1
    )
    notices = await fakes_get("/_admin/slack/ephemeral", team="T0ACME", user="U0MALLORY")
    assert "don't have access" in notices[-1]["text"]
    dms = await fakes_get("/_admin/slack/messages", team="T0ACME", channel="U0ALICE")
    asks = [m for m in dms if "asked to use the agent" in m["text"]]
    assert len(asks) >= 1
    buttons = asks[-1]["blocks"][-1]["elements"]
    assert [b["action_id"] for b in buttons] == ["access_request_invite", "access_request_dismiss"]


# ------------------------------------------------------------------ channel rule


async def test_channel_rule(acts: SlackActivities, env: Any) -> None:
    world = env.world
    bob = Requester(str(world.acme.user), "bob@acme.test", "active")
    assert (await acts.check_channel(req(world, "U0BOB", channel="C0BUGS"), bob)).allowed
    random = await acts.check_channel(req(world, "U0BOB", channel="C0RANDOM"), bob)
    assert (random.allowed, random.reason) == (False, "channel_not_allowlisted")
    assert (await acts.check_channel(req(world, "U0BOB", channel="D0BOB", channel_type="im"), bob)).allowed
    assert (
        await acts.check_channel(req(world, "U0BOB", channel="G0MPIM", channel_type="mpim"), bob)
    ).reason == "group_dm"


async def test_posting_in_a_channel_records_membership(acts: SlackActivities, env: Any) -> None:
    world = env.world
    quinn = q(
        world,
        "select m.id from app.org_memberships m join app.users u on u.id = m.user_id where u.email = 'quinn@acme.test'",
    )
    with psycopg.connect(world.url) as conn:
        conn.execute(
            "delete from app.channel_members where external_user_id = 'U0QUINN' and channel_id = (select id from app.channels where external_id = 'C0GENERAL')"
        )
        conn.commit()
    check = await acts.check_channel(
        req(world, "U0QUINN", channel="C0GENERAL"), Requester(str(quinn), "quinn@acme.test", "active")
    )
    assert check.allowed


# ------------------------------------------------------------------ replies, sync, home


async def test_acknowledge_replies_in_the_thread_exactly_once(acts: SlackActivities, env: Any) -> None:
    world = env.world
    async with httpx.AsyncClient() as client:
        posted = (
            await client.post(
                f"{FAKES}/slack/_ui/post",
                json={"team": "T0ACME", "user": "U0BOB", "channel": "C0BUGS", "text": "<@U0ACMEBOT> hello"},
            )
        ).json()["message"]
    request = req(world, "U0BOB", ts=posted["ts"], event_id=f"EvACK{posted['ts'].replace('.', '')}")
    bob = Requester(str(world.acme.user), "bob@acme.test", "active")
    first = await acts.acknowledge(request, bob)
    second = await acts.acknowledge(request, bob)  # an activity retry
    assert first is not None
    assert second is None
    messages = await fakes_get("/_admin/slack/messages", team="T0ACME", channel="C0BUGS")
    replies = [m for m in messages if m.get("thread_ts") == posted["ts"] and m.get("bot_id")]
    assert len(replies) == 1
    assert "Received" in replies[0]["text"]


async def test_sync_workspace(acts: SlackActivities, env: Any) -> None:
    world = env.world
    workspace = q(world, "select id from app.workspaces where external_id = 'T0ACME'")
    with psycopg.connect(world.url) as conn:
        conn.execute(
            "delete from app.channel_members where channel_id = (select id from app.channels where external_id = 'C0BUGS')"
        )
        conn.execute(
            "insert into app.channels (org_id, workspace_id, external_id, name) values (%s, %s, 'C0GHOST', 'ghost') on conflict do nothing",
            (world.acme.id, workspace),
        )
        conn.commit()
    result = await acts.sync_workspace(SyncRequest(str(world.acme.id), str(workspace), "T0ACME"))
    assert result.channels >= 4
    assert result.member_lists >= 1
    members = {
        r[0]
        for r in psycopg.connect(world.url).execute(
            "select external_user_id from app.channel_members where channel_id = (select id from app.channels where external_id = 'C0BUGS')"
        )
    }
    assert {"U0ALICE", "U0BOB", "U0QUINN"} <= members
    assert (
        q(world, "select archived from app.channels where external_id = 'C0GHOST'") is True
    )  # gone from Slack
    assert q(world, "select bot_is_member from app.channels where external_id = 'C0LEADS'") is True


async def test_publish_home(acts: SlackActivities, env: Any) -> None:
    workspace = q(env.world, "select id from app.workspaces where external_id = 'T0ACME'")
    assert (
        await acts.publish_home(HomeRequest(str(env.world.acme.id), str(workspace), "T0ACME", "U0ALICE"))
        == "published"
    )
    home = await fakes_get("/_admin/slack/home", team="T0ACME", user="U0ALICE")
    text = str(home["blocks"])
    assert "Acme Corp" in text
    assert "home_mode" in text
    await acts.publish_home(HomeRequest(str(env.world.acme.id), str(workspace), "T0ACME", "U0MALLORY"))
    outsider = await fakes_get("/_admin/slack/home", team="T0ACME", user="U0MALLORY")
    assert "home_request_access" in str(outsider["blocks"])

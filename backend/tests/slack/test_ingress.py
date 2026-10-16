"""Slack ingress (spec 14.2): verification, dedupe, routing, acknowledgement."""

import json
import time
import uuid
from dataclasses import asdict
from typing import Any
from urllib.parse import urlencode

import httpx
import psycopg
import pytest

from app.slack import signature
from tests.slack.conftest import SIGNING_SECRET, Stack, seed_slack_token
from tests.world import World


def signed(
    body: bytes, secret: str = SIGNING_SECRET, ts: int | None = None, content_type: str = "application/json"
) -> dict[str, str]:
    stamp = ts if ts is not None else int(time.time())
    return {
        "x-slack-request-timestamp": str(stamp),
        "x-slack-signature": signature.sign(secret, stamp, body),
        "content-type": content_type,
    }


def event_payload(event: dict[str, Any], team: str = "T0ACME", event_id: str | None = None) -> dict[str, Any]:
    return {
        "type": "event_callback",
        "team_id": team,
        "api_app_id": "A0AGENT",
        "event_id": event_id or f"Ev{uuid.uuid4().hex[:10]}",
        "event_time": int(time.time()),
        "event": event,
    }


async def post_event(api: httpx.AsyncClient, payload: dict[str, Any], **kwargs: Any) -> httpx.Response:
    body = json.dumps(payload).encode()
    return await api.post("/slack/events", content=body, headers=signed(body, **kwargs))


def q(world: World, query: str, params: tuple[Any, ...] = ()) -> Any:
    with psycopg.connect(world.url) as conn:
        row = conn.execute(query, params).fetchone()
        return row[0] if row and len(row) == 1 else row


def mention(
    user: str = "U0BOB", channel: str = "C0BUGS", text: str = "<@U0ACMEBOT> summarize #bugs"
) -> dict[str, Any]:
    return {"type": "app_mention", "user": user, "text": text, "ts": f"{time.time():.6f}", "channel": channel}


# ------------------------------------------------------------------ verification


async def test_url_verification(api: httpx.AsyncClient) -> None:
    response = await post_event(api, {"type": "url_verification", "challenge": "abc123", "token": "x"})
    assert response.json() == {"challenge": "abc123"}


@pytest.mark.parametrize(
    "kwargs", [{"secret": "wrong"}, {"ts": int(time.time()) - 400}], ids=["bad_signature", "stale_timestamp"]
)
async def test_unverifiable_requests_are_refused(
    api: httpx.AsyncClient, stack: Stack, kwargs: dict[str, Any]
) -> None:
    stack.temporal.started.clear()
    response = await post_event(api, event_payload(mention()), **kwargs)
    assert response.status_code == 401
    assert stack.temporal.started == []


async def test_missing_signature_headers(api: httpx.AsyncClient) -> None:
    response = await api.post(
        "/slack/events",
        content=b'{"type":"url_verification","challenge":"x"}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 401


async def test_oversized_and_malformed_payloads(api: httpx.AsyncClient) -> None:
    big = b"x" * (1024 * 1024 + 1)
    assert (await api.post("/slack/events", content=big, headers=signed(big))).status_code == 413
    bad = b"{not json"
    assert (await api.post("/slack/events", content=bad, headers=signed(bad))).status_code == 400


# ------------------------------------------------------------------ routing and dedupe


async def test_mention_starts_a_workflow_with_references_only(
    api: httpx.AsyncClient, stack: Stack, env: Any
) -> None:
    stack.temporal.started.clear()
    payload = event_payload(mention(text="<@U0ACMEBOT> secret plans for the reorg"))
    started = time.monotonic()
    response = await post_event(api, payload)
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert elapsed < 1.0  # acknowledged well within Slack's 3 seconds
    ((workflow, arg, workflow_id),) = stack.temporal.started
    assert (workflow, workflow_id) == ("AgentRequestWorkflow", f"slack-{payload['event_id']}")
    data = asdict(arg)
    assert data["org_id"] == str(env.world.acme.id)
    assert (data["channel"], data["user"], data["channel_type"], data["event_type"]) == (
        "C0BUGS",
        "U0BOB",
        "channel",
        "app_mention",
    )
    assert "secret plans" not in json.dumps(data)  # raw message text never enters workflow history


async def test_duplicate_events_are_acknowledged_once(api: httpx.AsyncClient, stack: Stack) -> None:
    stack.temporal.started.clear()
    payload = event_payload(mention())
    for _ in range(3):  # Slack retries
        assert (await post_event(api, payload)).status_code == 200
    assert len(stack.temporal.started) == 1


async def test_temporal_rejects_duplicate_workflow_ids_too(stack: Stack, api: httpx.AsyncClient) -> None:
    """Second dedupe layer: even without the Key Value claim, the workflow id is unique."""
    stack.temporal.started.clear()
    payload = event_payload(mention())
    await post_event(api, payload)
    if stack.kv is not None:
        await stack.kv.delete(f"ap:g:slack-event:{payload['event_id']}")
    assert (await post_event(api, payload)).status_code == 200
    assert len(stack.temporal.started) == 1


async def test_enqueue_failure_releases_the_claim_for_slacks_retry(
    api: httpx.AsyncClient, stack: Stack
) -> None:
    stack.temporal.started.clear()
    payload = event_payload(mention())
    stack.temporal.fail = True
    try:
        assert (await post_event(api, payload)).status_code == 500
    finally:
        stack.temporal.fail = False
    assert (await post_event(api, payload)).status_code == 200  # the retry goes through
    assert len(stack.temporal.started) == 1


async def test_unknown_and_uninstalled_workspaces_get_200(
    api: httpx.AsyncClient, stack: Stack, env: Any
) -> None:
    stack.temporal.started.clear()
    assert (await post_event(api, event_payload(mention(), team="T0UNKNOWN"))).status_code == 200
    with psycopg.connect(env.world.url) as conn:
        conn.execute("update app.workspaces set status = 'uninstalled' where external_id = 'T0GLOBEX'")
        conn.commit()
    try:
        assert (
            await post_event(
                api, event_payload(mention(user="U0DAVE", channel="C0GXGENERAL"), team="T0GLOBEX")
            )
        ).status_code == 200
    finally:
        with psycopg.connect(env.world.url) as conn:
            conn.execute("update app.workspaces set status = 'active' where external_id = 'T0GLOBEX'")
            conn.commit()
    assert stack.temporal.started == []


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "message",
            "channel_type": "channel",
            "channel": "C0BUGS",
            "user": "U0BOB",
            "text": "hi",
            "ts": "1.1",
        },
        {
            "type": "message",
            "channel_type": "im",
            "subtype": "message_changed",
            "channel": "D0BOB",
            "ts": "1.2",
        },
        {
            "type": "message",
            "channel_type": "im",
            "channel": "D0BOB",
            "bot_id": "B0ACMEBOT",
            "text": "my own reply",
            "ts": "1.3",
        },
        {
            "type": "app_mention",
            "channel": "C0BUGS",
            "bot_id": "B0OTHER",
            "text": "<@U0ACMEBOT>",
            "ts": "1.4",
        },
        {"type": "reaction_added", "user": "U0BOB"},
    ],
    ids=["channel_message", "edit", "own_dm_reply", "bot_mention", "unsubscribed"],
)
async def test_non_requests_are_ignored(api: httpx.AsyncClient, stack: Stack, event: dict[str, Any]) -> None:
    stack.temporal.started.clear()
    assert (await post_event(api, event_payload(event))).status_code == 200
    assert stack.temporal.started == []


async def test_direct_messages_start_a_workflow(api: httpx.AsyncClient, stack: Stack) -> None:
    stack.temporal.started.clear()
    await post_event(
        api,
        event_payload(
            {
                "type": "message",
                "channel_type": "im",
                "channel": "D0BOB",
                "user": "U0BOB",
                "text": "hello",
                "ts": "2.2",
            }
        ),
    )
    ((_, arg, _),) = stack.temporal.started
    assert arg.channel_type == "im"


async def test_app_home_opened_publishes_the_home(api: httpx.AsyncClient, stack: Stack) -> None:
    stack.temporal.started.clear()
    await post_event(
        api, event_payload({"type": "app_home_opened", "user": "U0BOB", "channel": "D0BOB", "tab": "home"})
    )
    await post_event(
        api,
        event_payload({"type": "app_home_opened", "user": "U0BOB", "channel": "D0BOB", "tab": "messages"}),
    )
    assert [w for w, _, _ in stack.temporal.started] == ["PublishHomeWorkflow"]


# ------------------------------------------------------------------ channel and membership events


async def test_membership_events_update_postgres_and_the_cache(
    api: httpx.AsyncClient, stack: Stack, env: Any
) -> None:
    world = env.world
    await post_event(
        api,
        event_payload(
            {"type": "member_joined_channel", "user": "U0FRANKA", "channel": "C0BUGS", "channel_type": "C"}
        ),
    )
    channel = q(world, "select id from app.channels where external_id = 'C0BUGS'")
    assert (
        q(
            world,
            "select count(*) from app.channel_members where channel_id = %s and external_user_id = 'U0FRANKA'",
            (channel,),
        )
        == 1
    )
    if stack.kv is not None:
        assert await stack.kv.sismember(f"ap:t:{world.acme.id}:chm:{channel}", "U0FRANKA")
    await post_event(
        api,
        event_payload(
            {"type": "member_left_channel", "user": "U0FRANKA", "channel": "C0BUGS", "channel_type": "C"}
        ),
    )
    assert (
        q(
            world,
            "select count(*) from app.channel_members where channel_id = %s and external_user_id = 'U0FRANKA'",
            (channel,),
        )
        == 0
    )
    if stack.kv is not None:
        assert not await stack.kv.sismember(f"ap:t:{world.acme.id}:chm:{channel}", "U0FRANKA")


async def test_bot_joining_or_leaving_updates_bot_membership(api: httpx.AsyncClient, env: Any) -> None:
    await post_event(
        api,
        event_payload(
            {"type": "member_joined_channel", "user": "U0ACMEBOT", "channel": "C0RANDOM", "channel_type": "C"}
        ),
    )
    assert q(env.world, "select bot_is_member from app.channels where external_id = 'C0RANDOM'") is True
    await post_event(
        api,
        event_payload(
            {"type": "member_left_channel", "user": "U0ACMEBOT", "channel": "C0RANDOM", "channel_type": "C"}
        ),
    )
    assert q(env.world, "select bot_is_member from app.channels where external_id = 'C0RANDOM'") is False


async def test_channel_lifecycle_events(api: httpx.AsyncClient, env: Any, stack: Stack) -> None:
    world = env.world
    await post_event(
        api,
        event_payload(
            {"type": "channel_created", "channel": {"id": "C0NEWONE", "name": "launch", "created": 1}}
        ),
    )
    assert q(world, "select name, is_allowlisted from app.channels where external_id = 'C0NEWONE'") == (
        "launch",
        False,
    )
    await post_event(
        api,
        event_payload(
            {"type": "channel_rename", "channel": {"id": "C0NEWONE", "name": "launch-v2", "created": 1}}
        ),
    )
    assert q(world, "select name from app.channels where external_id = 'C0NEWONE'") == "launch-v2"
    # Archiving the delivery channel of a schedule pauses it (database trigger).
    schedule = world.acme.ids["schedules"]
    with psycopg.connect(world.url) as conn:
        conn.execute(
            "update app.schedules set status = 'active', pause_reason = null where id = %s", (schedule,)
        )
        conn.execute(
            "update app.channels set archived = false, is_allowlisted = true where external_id = 'C0BUGS'"
        )
        conn.commit()
    started = len(stack.temporal.started)
    await post_event(api, event_payload({"type": "channel_archive", "channel": "C0BUGS", "user": "U0ALICE"}))
    assert q(world, "select archived from app.channels where external_id = 'C0BUGS'") is True
    assert (
        q(world, "select status || ':' || pause_reason from app.schedules where id = %s", (schedule,))
        == "paused:access_lost"
    )
    # Temporal stops firing it and the admins hear about it.
    assert [(w, a) for w, a, _ in stack.temporal.started[started:]] == [
        ("ScheduleSyncWorkflow", str(world.acme.id))
    ]
    await post_event(
        api, event_payload({"type": "channel_unarchive", "channel": "C0BUGS", "user": "U0ALICE"})
    )
    assert q(world, "select archived from app.channels where external_id = 'C0BUGS'") is False


async def test_uninstall_revokes_the_connection_and_deletes_the_token(
    api: httpx.AsyncClient, stack: Stack, env: Any
) -> None:
    world = env.world
    connection = await seed_slack_token(stack, world.globex.id, "T0GLOBEX")
    # User token revocations are ignored; bot token revocation uninstalls.
    await post_event(
        api,
        event_payload(
            {"type": "tokens_revoked", "tokens": {"oauth": ["U0DAVE"], "bot": []}}, team="T0GLOBEX"
        ),
    )
    assert q(world, "select status from app.workspaces where external_id = 'T0GLOBEX'") == "active"
    await post_event(api, event_payload({"type": "app_uninstalled"}, team="T0GLOBEX"))
    assert (
        q(
            world,
            "select status from app.workspaces where external_id = 'T0GLOBEX' order by created_at desc limit 1",
        )
        == "uninstalled"
    )
    assert q(world, "select status from app.connections where id = %s", (connection,)) == "revoked"
    assert (
        q(world, "select count(*) from app.connection_secrets where connection_id = %s", (connection,)) == 0
    )
    assert (
        q(
            world,
            "select count(*) from app.audit_log where action = 'slack.uninstalled' and org_id = %s",
            (world.globex.id,),
        )
        == 1
    )
    with psycopg.connect(world.url) as conn:  # restore for other tests
        conn.execute("update app.workspaces set status = 'active' where external_id = 'T0GLOBEX'")
        conn.commit()


# ------------------------------------------------------------------ interactions


async def post_interaction(api: httpx.AsyncClient, payload: dict[str, Any], **kwargs: Any) -> httpx.Response:
    body = urlencode({"payload": json.dumps(payload)}).encode()
    return await api.post(
        "/slack/interactions",
        content=body,
        headers=signed(body, content_type="application/x-www-form-urlencoded", **kwargs),
    )


def action_payload(action: dict[str, Any], user: str = "U0BOB", team: str = "T0ACME") -> dict[str, Any]:
    return {
        "type": "block_actions",
        "user": {"id": user},
        "team": {"id": team},
        "container": {"type": "view"},
        "actions": [{"action_ts": f"{time.time():.6f}", **action}],
    }


async def test_interactions_need_a_valid_signature(api: httpx.AsyncClient) -> None:
    response = await post_interaction(
        api, action_payload({"action_id": "home_mode", "selected_option": {"value": "auto"}}), secret="nope"
    )
    assert response.status_code == 401


async def test_home_mode_change_is_stored_and_republished(
    api: httpx.AsyncClient, stack: Stack, env: Any
) -> None:
    stack.temporal.started.clear()
    assert (
        await post_interaction(
            api, action_payload({"action_id": "home_mode", "selected_option": {"value": "auto"}})
        )
    ).status_code == 200
    bob = env.world.acme.user
    assert q(env.world, "select mode from app.user_preferences where membership_id = %s", (bob,)) == "auto"
    assert [w for w, _, _ in stack.temporal.started] == ["PublishHomeWorkflow"]
    await post_interaction(
        api, action_payload({"action_id": "home_mode", "selected_option": {"value": "superuser"}})
    )
    assert (
        q(env.world, "select mode from app.user_preferences where membership_id = %s", (bob,)) == "auto"
    )  # junk ignored


async def test_unknown_people_cannot_change_preferences(api: httpx.AsyncClient, env: Any) -> None:
    await post_interaction(
        api,
        action_payload({"action_id": "home_mode", "selected_option": {"value": "auto"}}, user="U0MALLORY"),
    )
    assert (
        q(
            env.world,
            "select count(*) from app.user_preferences p join app.platform_identities pi on pi.membership_id = p.membership_id where pi.external_user_id = 'U0MALLORY'",
        )
        == 0
    )

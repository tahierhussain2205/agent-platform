"""The Slack stand-in behaves like the parts of Slack the platform relies on."""

import hashlib
import hmac
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from fakes.config import Config
from fakes.main import create_app

FIXTURE = Path(__file__).resolve().parents[2] / "backend" / "app" / "seed" / "fixture.json"
SECRET = "local-slack-signing-secret"
ACME = {"authorization": "Bearer xoxb-local-T0ACME"}


class Receiver:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.fail_next = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next > 0:
            self.fail_next -= 1
            return httpx.Response(500)
        return httpx.Response(200)

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith("/events")]

    def interactions(self) -> list[dict[str, Any]]:
        return [
            json.loads(parse_qs(r.content.decode())["payload"][0])
            for r in self.requests
            if r.url.path.endswith("/interactions")
        ]


@pytest.fixture
def receiver() -> Receiver:
    return Receiver()


@pytest.fixture
def client(tmp_path: Path, receiver: Receiver) -> Iterator[TestClient]:
    config = Config(
        state_dir=tmp_path,
        seed_fixture=FIXTURE,
        slack_events_url="http://api/slack/events",
        slack_interactions_url="http://api/slack/interactions",
    )
    app = create_app(config)
    deliverer = app.state.slack_deliverer
    deliverer._http = httpx.AsyncClient(transport=httpx.MockTransport(receiver.handler))
    deliverer.retry_delays = (0.01, 0.01, 0.01)
    with TestClient(app) as c:
        yield c


def api(client: TestClient, method: str, headers: dict[str, str] = ACME, **params: Any) -> httpx.Response:
    return client.post(
        f"/slack/api/{method}",
        data={k: json.dumps(v) if isinstance(v, list | dict) else v for k, v in params.items()},
        headers=headers,
    )


def drain(client: TestClient) -> None:
    client.post("/_admin/slack/deliveries/drain")


def test_auth_and_errors(client: TestClient) -> None:
    assert api(client, "auth.test").json()["team_id"] == "T0ACME"
    assert api(client, "auth.test", headers={"authorization": "Bearer xoxb-nope"}).json() == {
        "ok": False,
        "error": "invalid_auth",
    }
    assert api(client, "nope.method").json()["error"] == "unknown_method"


def test_oauth_install_flow(client: TestClient) -> None:
    page = client.get(
        "/slack/oauth/v2/authorize",
        params={"client_id": "local.client", "redirect_uri": "http://api/cb", "state": "s1", "scope": "chat:write"},
    )
    assert "Newco" in page.text
    response = client.post(
        "/slack/oauth/v2/authorize",
        data={
            "redirect_uri": "http://api/cb",
            "state": "s1",
            "scope": "chat:write",
            "team": "T0NEWCO",
            "decision": "allow",
        },
        follow_redirects=False,
    )
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["state"] == ["s1"]
    code = query["code"][0]
    bad = client.post(
        "/slack/api/oauth.v2.access",
        data={"code": code, "redirect_uri": "http://api/cb"},
        auth=("local.client", "wrong"),
    )
    assert bad.json()["error"] == "invalid_client"
    good = client.post(
        "/slack/api/oauth.v2.access",
        data={"code": code, "redirect_uri": "http://api/cb"},
        auth=("local.client", "local-slack-client-secret"),
    )
    body = good.json()
    assert (body["ok"], body["token_type"], body["team"]["id"], body["bot_user_id"]) == (
        True,
        "bot",
        "T0NEWCO",
        "U0NEWCOBOT",
    )
    assert body["access_token"].startswith("xoxb-T0NEWCO-")
    again = client.post(
        "/slack/api/oauth.v2.access",
        data={"code": code, "redirect_uri": "http://api/cb"},
        auth=("local.client", "local-slack-client-secret"),
    )
    assert again.json()["error"] == "invalid_code"  # codes are single use
    cancelled = client.post(
        "/slack/oauth/v2/authorize",
        data={"redirect_uri": "http://api/cb", "state": "s2", "team": "T0NEWCO", "decision": "cancel"},
        follow_redirects=False,
    )
    assert "error=access_denied" in cancelled.headers["location"]


def test_conversations_respect_bot_visibility(client: TestClient) -> None:
    channels = {
        c["name"]: c
        for c in api(client, "conversations.list", types="public_channel,private_channel").json()["channels"]
    }
    assert channels["bugs"]["is_member"] is True
    assert channels["leads-private"]["is_private"] is True
    assert channels["random"]["is_member"] is False
    assert api(client, "conversations.history", channel="C0RANDOM").json()["error"] == "not_in_channel"
    members = api(client, "conversations.members", channel="C0BUGS").json()["members"]
    assert set(members) == {"U0ALICE", "U0BOB", "U0QUINN"}
    page = api(client, "conversations.list", types="public_channel", limit=2).json()
    assert len(page["channels"]) == 2
    assert page["response_metadata"]["next_cursor"] == "2"


def test_history_threads_and_replies(client: TestClient) -> None:
    history = api(client, "conversations.history", channel="C0BUGS").json()["messages"]
    checkout = next(m for m in history if "Checkout fails" in m["text"])
    assert checkout["reply_count"] == 2
    replies = api(client, "conversations.replies", channel="C0BUGS", ts=checkout["ts"]).json()["messages"]
    assert [m["user"] for m in replies] == ["U0QUINN", "U0BOB", "U0BOB"]


def test_message_metadata_round_trip(client: TestClient) -> None:
    """Posts can carry metadata (we put idempotency keys there); reads return it only on request."""
    meta = {"event_type": "agent_post", "event_payload": {"key": "task:1:report"}}
    root = api(client, "chat.postMessage", channel="C0BUGS", text="root").json()["ts"]
    api(client, "chat.postMessage", channel="C0BUGS", text="reply", thread_ts=root, metadata=meta)
    replies = api(client, "conversations.replies", channel="C0BUGS", ts=root, include_all_metadata="true").json()
    assert replies["messages"][1]["metadata"] == meta
    plain = api(client, "conversations.replies", channel="C0BUGS", ts=root).json()
    assert "metadata" not in plain["messages"][1]
    bad = api(client, "chat.postMessage", channel="C0BUGS", text="x", metadata={"event_payload": {}}).json()
    assert bad["error"] == "invalid_metadata_format"


def test_mentions_emit_signed_app_mention_events(client: TestClient, receiver: Receiver) -> None:
    client.post(
        "/slack/_ui/post",
        json={"team": "T0ACME", "user": "U0BOB", "channel": "C0BUGS", "text": "<@U0ACMEBOT> summarize today"},
    )
    client.post(
        "/slack/_ui/post", json={"team": "T0ACME", "user": "U0BOB", "channel": "C0BUGS", "text": "no mention here"}
    )
    drain(client)
    (event,) = receiver.events()
    assert (
        event["type"],
        event["team_id"],
        event["event"]["type"],
        event["event"]["channel"],
        event["event"]["user"],
    ) == ("event_callback", "T0ACME", "app_mention", "C0BUGS", "U0BOB")
    request = receiver.requests[0]
    ts = request.headers["x-slack-request-timestamp"]
    expected = "v0=" + hmac.new(SECRET.encode(), f"v0:{ts}:".encode() + request.content, hashlib.sha256).hexdigest()
    assert request.headers["x-slack-signature"] == expected


def test_no_mention_events_where_the_bot_is_not_a_member(client: TestClient, receiver: Receiver) -> None:
    client.post(
        "/slack/_ui/post", json={"team": "T0ACME", "user": "U0BOB", "channel": "C0RANDOM", "text": "<@U0ACMEBOT> hi"}
    )
    drain(client)
    assert receiver.events() == []


def test_dms_emit_message_im_and_the_bot_can_reply(client: TestClient, receiver: Receiver) -> None:
    state = client.get("/slack/_ui/state", params={"team": "T0ACME", "user": "U0BOB"}).json()
    dm = state["dm"]["id"]
    client.post("/slack/_ui/post", json={"team": "T0ACME", "user": "U0BOB", "channel": dm, "text": "hello"})
    drain(client)
    assert receiver.events()[0]["event"]["channel_type"] == "im"
    sent = api(client, "chat.postMessage", channel="U0BOB", text="hi Bob").json()
    assert sent["channel"] == dm
    assert api(client, "conversations.open", users="U0BOB").json()["channel"]["id"] == dm
    assert api(client, "conversations.open", users="U0NOBODY").json()["error"] == "user_not_found"
    assert api(client, "conversations.open").json()["error"] == "not_enough_users"
    assert (
        client.get("/_admin/slack/messages", params={"team": "T0ACME", "channel": "U0BOB"}).json()[-1]["text"]
        == "hi Bob"
    )


def test_post_update_ephemeral_and_threads(client: TestClient) -> None:
    sent = api(client, "chat.postMessage", channel="C0BUGS", text="status", thread_ts="123.456").json()
    assert api(client, "chat.update", channel="C0BUGS", ts=sent["ts"], text="status 2").json()["text"] == "status 2"
    assert api(client, "chat.postMessage", channel="C0RANDOM", text="x").json()["error"] == "not_in_channel"
    assert api(client, "chat.postEphemeral", channel="C0BUGS", user="U0BOB", text="just you").json()["ok"]
    assert (
        api(client, "chat.postEphemeral", channel="C0BUGS", user="U0FRANKA", text="x").json()["error"]
        == "user_not_in_channel"
    )
    assert (
        client.get("/_admin/slack/ephemeral", params={"team": "T0ACME", "user": "U0BOB"}).json()[0]["text"]
        == "just you"
    )


def test_users_and_home(client: TestClient) -> None:
    user = api(client, "users.info", user="U0MALLORY").json()["user"]
    assert user["profile"]["email"] == "mallory@acme.test"
    assert api(client, "users.lookupByEmail", email="BOB@acme.test").json()["user"]["id"] == "U0BOB"
    view = {"type": "home", "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]}
    assert api(client, "views.publish", user_id="U0BOB", view=view).json()["ok"]
    assert (
        client.get("/_admin/slack/home", params={"team": "T0ACME", "user": "U0BOB"}).json()["blocks"] == view["blocks"]
    )


def test_membership_events_and_interactions(client: TestClient, receiver: Receiver) -> None:
    client.post("/slack/_ui/join", json={"team": "T0ACME", "channel": "C0BUGS", "user": "U0FRANKA", "join": True})
    client.post(
        "/slack/_ui/action",
        json={
            "team": "T0ACME",
            "user": "U0ALICE",
            "channel": "D0ALICE",
            "ts": "1.2",
            "action_id": "access_request_invite",
            "value": "req-1",
        },
    )
    drain(client)
    assert receiver.events()[0]["event"] == {
        **receiver.events()[0]["event"],
        "type": "member_joined_channel",
        "user": "U0FRANKA",
        "channel": "C0BUGS",
    }
    (interaction,) = receiver.interactions()
    assert (
        interaction["type"],
        interaction["actions"][0]["action_id"],
        interaction["actions"][0]["value"],
        interaction["user"]["id"],
    ) == ("block_actions", "access_request_invite", "req-1", "U0ALICE")


def test_failed_event_deliveries_are_retried_with_retry_headers(client: TestClient, receiver: Receiver) -> None:
    receiver.fail_next = 2
    client.post(
        "/slack/_ui/post", json={"team": "T0ACME", "user": "U0BOB", "channel": "C0BUGS", "text": "<@U0ACMEBOT> again"}
    )
    drain(client)
    assert [r.headers.get("x-slack-retry-num") for r in receiver.requests] == [None, "1", "2"]
    assert len({json.loads(r.content)["event_id"] for r in receiver.requests}) == 1  # same event id on every retry


def test_rate_limits(client: TestClient) -> None:
    client.post("/_admin/slack/rate-limits", json={"chat.postMessage": 1})
    first = api(client, "chat.postMessage", channel="C0BUGS", text="1")
    second = api(client, "chat.postMessage", channel="C0BUGS", text="2")
    assert first.json()["ok"]
    assert second.status_code == 429
    assert second.headers["retry-after"] == "1"


def test_uninstall_revokes_tokens(client: TestClient, receiver: Receiver) -> None:
    client.post("/_admin/slack/uninstall", json={"team": "T0ACME"})
    assert [e["event"]["type"] for e in receiver.events()] == ["app_uninstalled", "tokens_revoked"]
    assert api(client, "auth.test").json()["error"] == "invalid_auth"

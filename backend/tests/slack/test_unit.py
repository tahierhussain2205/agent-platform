"""Slack building blocks: signatures, Block Kit rendering, client retries, rate limiting."""

import json
import time
from typing import Any

import httpx
import pytest

from app import kv
from app.slack import signature
from app.slack.adapter import (
    BLOCK_LIMIT,
    SECTION_LIMIT,
    ApprovalRequest,
    ChannelAdapter,
    DirectMessage,
    Ephemeral,
    Progress,
    Reply,
    Report,
    escape,
    render,
    split_text,
)
from app.slack.client import SlackClient, SlackError
from app.slack.limiter import SlackRateLimiter

SECRET = "s3cret"

# ------------------------------------------------------------------ signatures


def test_signature_round_trip() -> None:
    body = b'{"type":"event_callback"}'
    ts = int(time.time())
    signature.verify(SECRET, str(ts), signature.sign(SECRET, ts, body), body)


@pytest.mark.parametrize(
    ("ts_offset", "secret", "body_suffix", "headers_present"),
    [
        (0, "wrong", b"", True),
        (-301, SECRET, b"", True),
        (301, SECRET, b"", True),
        (0, SECRET, b" ", True),
        (0, SECRET, b"", False),
    ],
    ids=["wrong_secret", "stale", "future", "tampered_body", "missing_headers"],
)
def test_signature_rejections(ts_offset: int, secret: str, body_suffix: bytes, headers_present: bool) -> None:
    body = b"payload"
    ts = int(time.time()) + ts_offset
    sig = signature.sign(secret, ts, body)
    with pytest.raises(signature.SlackSignatureError):
        signature.verify(
            SECRET, str(ts) if headers_present else None, sig if headers_present else None, body + body_suffix
        )


def test_signature_bad_timestamp() -> None:
    with pytest.raises(signature.SlackSignatureError, match="bad timestamp"):
        signature.verify(SECRET, "yesterday", "v0=abc", b"x")


# ------------------------------------------------------------------ Block Kit


def test_split_text_respects_the_section_limit_and_keeps_content() -> None:
    text = "\n\n".join(f"Paragraph {i}: " + "word " * 120 for i in range(20))
    chunks = split_text(text)
    assert all(len(c) <= SECTION_LIMIT for c in chunks)
    assert " ".join(" ".join(chunks).split()) == " ".join(text.split())


def test_split_text_hard_splits_unbroken_text() -> None:
    chunks = split_text("x" * 7000)
    assert [len(c) for c in chunks] == [3000, 3000, 1000]


def test_reply_renders_sections_in_the_thread() -> None:
    ((method, params),) = render(Reply("C1", "hello *world*", "111.2"))
    assert method == "chat.postMessage"
    assert params["thread_ts"] == "111.2"
    assert params["blocks"] == [{"type": "section", "text": {"type": "mrkdwn", "text": "hello *world*"}}]


def test_long_reports_split_across_messages() -> None:
    report = Report(
        "C1",
        "Daily report",
        [(f"Section {i}", "line " * 100) for i in range(80)],
        "t1",
        footer="Numbers computed by code",
    )
    calls = render(report)
    assert len(calls) > 1
    assert all(len(params["blocks"]) <= BLOCK_LIMIT for _, params in calls)
    assert all(params["thread_ts"] == "t1" for _, params in calls)
    assert calls[0][1]["blocks"][0]["type"] == "header"


def test_other_intents() -> None:
    ((method, params),) = render(Ephemeral("C1", "U1", "only you"))
    assert (method, params["user"]) == ("chat.postEphemeral", "U1")
    ((method, params),) = render(
        DirectMessage(
            "U9", "access request", actions=[("invite", "Invite", "r1"), ("dismiss", "Dismiss", "r1")]
        )
    )
    actions = params["blocks"][-1]["elements"]
    assert (method, params["channel"], actions[0]["style"], "style" in actions[1]) == (
        "chat.postMessage",
        "U9",
        "primary",
        False,
    )
    ((method, params),) = render(Progress("C1", "Working", ts="99.1"))
    assert (method, params["ts"]) == ("chat.update", "99.1")
    ((method, params),) = render(
        ApprovalRequest("C1", "1.1", "Create ticket ABC", "appr-1", "reversible_write", "Expires in 24 hours")
    )
    buttons = params["blocks"][-1]["elements"]
    assert [b["action_id"] for b in buttons] == ["approval_approve", "approval_reject"]
    assert all(b["value"] == "appr-1" for b in buttons)


def test_escape_neutralizes_mentions_and_links_but_keeps_quotes() -> None:
    raw = "<!channel> ping <@U0BOB> see <https://evil.example|your login> & <#C0BUGS>\n> quoted"
    escaped = escape(raw)
    assert "<" not in escaped
    assert escaped == (
        "&lt;!channel> ping &lt;@U0BOB> see &lt;https://evil.example|your login> &amp; &lt;#C0BUGS>\n> quoted"
    )
    assert escape("&lt;!here>") == "&amp;lt;!here>"  # an entity the model wrote stays text too


def test_app_home_escapes_names_people_and_models_wrote() -> None:
    from app.workflows.slack_activities import home_view

    view = home_view(
        "Acme <!channel>", "bob@acme.test", "ask", "ask", "auto", None, False, "http://console",
        [{"id": "s1", "name": "<!here> daily", "when": "Daily", "state": "active", "status": "active"}],
    )  # fmt: skip

    def mrkdwn(node: Any) -> list[str]:  # plain_text is never parsed by Slack, so it is left as written
        if isinstance(node, dict):
            own = [node["text"]] if node.get("type") == "mrkdwn" else []
            return own + [t for v in node.values() for t in mrkdwn(v)]
        return [t for v in node for t in mrkdwn(v)] if isinstance(node, list) else []

    texts = mrkdwn(view)
    assert not [t for t in texts if "<!channel>" in t or "<!here>" in t]
    assert any("&lt;!here> daily" in t for t in texts)
    assert any("&lt;!channel>" in t for t in texts)


def test_fallback_text_is_bounded() -> None:
    ((_, params),) = render(Reply("C1", "y" * 50000))
    assert len(params["text"]) <= 3000


# ------------------------------------------------------------------ client


def _client(handler: Any) -> SlackClient:
    return SlackClient(
        "https://slack.test/api", http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


async def test_client_sends_form_with_bearer_and_json_blocks() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "ts": "1.1"})

    await _client(handler).call(
        "chat.postMessage",
        "xoxb-1",
        None,
        channel="C1",
        blocks=[{"type": "divider"}],
        unfurl_links=False,
        skip=None,
    )
    request = seen[0]
    assert request.headers["authorization"] == "Bearer xoxb-1"
    form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
    assert "skip" not in form
    assert form["unfurl_links"] == "false"
    assert json.loads(httpx.QueryParams(request.content.decode())["blocks"]) == [{"type": "divider"}]


async def test_client_raises_slack_errors() -> None:
    client = _client(lambda r: httpx.Response(200, json={"ok": False, "error": "channel_not_found"}))
    with pytest.raises(SlackError) as info:
        await client.call("chat.postMessage", "xoxb-1", channel="C404")
    assert info.value.error == "channel_not_found"


async def test_client_retries_429_after_retry_after() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(
                429, headers={"Retry-After": "0"}, json={"ok": False, "error": "ratelimited"}
            )
        return httpx.Response(200, json={"ok": True})

    assert (await _client(handler).call("users.info", "xoxb", user="U1"))["ok"]
    assert calls["n"] == 3


async def test_client_gives_up_after_max_retries() -> None:
    client = _client(lambda r: httpx.Response(429, headers={"Retry-After": "0"}))
    with pytest.raises(SlackError, match="ratelimited"):
        await client.call("users.info", "xoxb", user="U1")


async def test_paginate_follows_cursors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        cursor = httpx.QueryParams(request.content.decode()).get("cursor")
        if not cursor:
            return httpx.Response(
                200, json={"ok": True, "members": ["U1", "U2"], "response_metadata": {"next_cursor": "c2"}}
            )
        return httpx.Response(
            200, json={"ok": True, "members": ["U3"], "response_metadata": {"next_cursor": ""}}
        )

    assert [
        m
        async for m in _client(handler).paginate(
            "conversations.members", "members", "xoxb", "T1", channel="C1"
        )
    ] == ["U1", "U2", "U3"]


# ------------------------------------------------------------------ limiter and coalescing


@pytest.fixture
async def redis() -> Any:
    client = kv.make_client("redis://localhost:56379/12")
    try:
        await client.ping()
    except Exception:
        pytest.skip("Key Value not reachable")
    yield client
    await client.aclose()


async def test_limiter_spreads_bursts_across_seconds(redis: Any) -> None:
    limiter = SlackRateLimiter(redis)
    team = f"T{time.time_ns()}"
    started = time.monotonic()
    for _ in range(3):
        await limiter.acquire(team, "conversations.list")  # 1 per second
    assert time.monotonic() - started >= 1.0


async def test_limiter_gives_up_beyond_max_wait(redis: Any) -> None:
    limiter = SlackRateLimiter(redis, max_wait_seconds=0.1)
    team = f"T{time.time_ns()}"
    await limiter.acquire(team, "conversations.list")
    with pytest.raises(TimeoutError):
        await limiter.acquire(team, "conversations.list")


async def test_progress_updates_are_coalesced(redis: Any) -> None:
    """An update that has to wait for a rate limit slot is dropped if a newer update to the same
    message arrived meanwhile; the newer one is sent."""
    import asyncio

    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(httpx.QueryParams(request.content.decode())["text"])
        return httpx.Response(200, json={"ok": True})

    limiter = SlackRateLimiter(redis)
    client = SlackClient(
        "https://slack.test/api",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        limiter=limiter,
    )
    adapter = ChannelAdapter(client, redis)
    team = f"T{time.time_ns()}"
    for _ in range(5):  # use up this second's chat budget
        await limiter.acquire(team, "chat.update")
    older = asyncio.create_task(adapter.send(team, "xoxb", Progress("C1", "older", ts="5.5")))
    await asyncio.sleep(0.05)  # the older update has its version and is waiting for a slot
    newer = asyncio.create_task(adapter.send(team, "xoxb", Progress("C1", "newer", ts="5.5")))
    await asyncio.gather(older, newer)
    assert sent == ["newer"]
    # Updates to different messages are independent.
    await adapter.send(team, "xoxb", Progress("C1", "other message", ts="6.6"))
    assert sent == ["newer", "other message"]

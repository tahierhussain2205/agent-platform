"""The Slack tool adapter against the Slack stand-in, redaction, and operation metadata."""

import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.policy.redaction import redact
from app.tools.base import RefBook, ToolContext, cap, definitions
from app.tools.registry import Registry
from app.tools.slack import OPERATIONS, SlackAdapter, plain
from tests.stack import FAKES, Stack, fakes_up

needs_fakes = pytest.mark.skipif(not fakes_up(), reason="Slack stand-in not reachable (run `make infra`)")


# ------------------------------------------------------------------ pure parts


def test_operation_tiers_and_definitions() -> None:
    assert {n: o.tier for n, o in OPERATIONS.items()} == {
        "list_channels": "read",
        "read_channel": "read",
        "read_thread": "read",
        "channel_stats": "read",
        "post_message": "irreversible",
    }
    # channel_stats is internal (report figures): never offered to a model, even when asked for.
    tools = definitions(SlackAdapter(), ["read_channel", "read_thread", "channel_stats"])
    assert [t["name"] for t in tools] == ["slack_read_channel", "slack_read_thread"]
    assert tools[0]["input_schema"]["required"] == ["channel"]
    registry = Registry()
    registry.register(SlackAdapter())
    found = registry.lookup("slack_post_message")
    assert found is not None
    assert found[1].outward_facing
    assert registry.lookup("slack_delete_everything") is None


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("token xoxb-123456789012-abcdefghij", "slack token"),
        ("ghp_" + "a" * 36, "github token"),
        ("key sk-ant-api03-abcdefghijklmnop", "anthropic key"),
        ("AKIAABCDEFGHIJKLMNOP", "aws access key"),
        ("postgres://app:s3cretpass@db.internal:5432/x", "password in url"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123", "bearer token"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----", "private key"),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop", "jwt"),
        ('password: "hunter2hunter2"', "secret"),
    ],
)
def test_redaction(text: str, label: str) -> None:
    redacted, count = redact(f"before {text} after")
    assert f"[redacted {label}]" in redacted
    assert count >= 1
    assert redacted.startswith("before ")


def test_redaction_leaves_ordinary_text_alone() -> None:
    text = "Checkout fails with a 500 for order 4411; see https://status.example.com/incidents/12"
    assert redact(text) == (text, 0)


def test_plain_text_normalisation() -> None:
    names = {"U0ALICE": "Alice Admin"}
    raw = "<@U0ALICE> see <#C0GENERAL|general>, <!here> <https://x.test/a|the doc> &amp; <https://y.test>\nok"
    assert (
        plain(raw, names)
        == "@Alice Admin see #general, @here the doc (https://x.test/a) & https://y.test ⏎ ok"
    )


def test_cap_truncates_on_a_line_boundary() -> None:
    text = "\n".join(f"line {i}" for i in range(5000))
    capped = cap(text, 100)
    assert len(capped) < 160
    assert capped.endswith("(output truncated; narrow the request to see more)")
    assert cap("short") == "short"


def test_refbook_dedupes() -> None:
    from app.tools.base import Source

    book = RefBook()
    a = Source("slack:C1:1.1", "C1", "bugs", "public", "1.1")
    assert (book.add(a), book.add(a), book.add(Source("slack:C1:1.2", "C1", "bugs", "public", "1.2"))) == (
        "S1",
        "S1",
        "S2",
    )
    assert book.get("[S2]") is not None
    assert book.get("S9") is None


# ------------------------------------------------------------------ against the stand-in


async def ui_post(user: str, channel: str, text: str, thread_ts: str | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{FAKES}/slack/_ui/post",
            json={"team": "T0ACME", "user": user, "channel": channel, "text": text, "thread_ts": thread_ts},
        )
    message: dict[str, Any] = r.json()["message"]
    return message


def tool_ctx(stack: Stack, who: str = "bob") -> ToolContext:
    world = stack.env.world
    return ToolContext(
        org_id=world.acme.id,
        membership_id=world.acme.user if who == "bob" else world.acme.admin,
        workspace_id=world.acme.workspace,
        team_id="T0ACME",
        slack_user_id="U0BOB",
        token="xoxb-local-T0ACME",
        pool=stack.pool,
        slack=stack.slack,
        kv=stack.kv,
    )


@needs_fakes
async def test_read_channel_in_a_window_with_threads_names_and_refs(stack: Stack) -> None:
    tag = uuid.uuid4().hex[:6]
    since = datetime.fromtimestamp(time.time() - 1, UTC).isoformat()
    root = await ui_post("U0QUINN", "C0BUGS", f"Search is slow {tag}, see <@U0ALICE>")
    await ui_post("U0BOB", "C0BUGS", f"Confirmed {tag} on EU", thread_ts=root["ts"])
    await ui_post("U0ALICE", "C0BUGS", f"Leaked xoxb-123456789012-abcdefghij {tag}")
    ctx = tool_ctx(stack)
    result = await SlackAdapter().execute(ctx, "read_channel", {"channel": "#bugs", "start": since})
    assert not result.is_error
    lines = result.text.splitlines()
    assert lines[0].startswith("Messages in #bugs from ")
    assert lines[0].endswith("(2 top level):")  # older seeded messages fall outside the window
    assert lines[1].startswith("[S1] (")
    assert f"Quinn Tester: Search is slow {tag}, see @Alice Admin" in lines[1]
    assert lines[2].startswith("  ↳ [S2] (")
    assert "Bob Builder: Confirmed" in lines[2]
    assert "[redacted slack token]" in lines[3]
    assert "xoxb-1234" not in result.text
    source = ctx.refs.get("S1")
    assert source is not None
    assert (source.channel_external_id, source.channel_name, source.visibility, source.ts) == (
        "C0BUGS", "bugs", "public", root["ts"],
    )  # fmt: skip


@needs_fakes
async def test_read_thread_and_errors(stack: Stack) -> None:
    root = await ui_post("U0BOB", "C0GENERAL", "Thread root")
    await ui_post("U0ALICE", "C0GENERAL", "A reply", thread_ts=root["ts"])
    adapter = SlackAdapter()
    ctx = tool_ctx(stack)
    thread = await adapter.execute(ctx, "read_thread", {"channel": "general", "ts": root["ts"]})
    assert thread.text.splitlines()[1:]
    assert "Bob Builder: Thread root" in thread.text
    assert "↳ [S2]" in thread.text
    assert (await adapter.execute(ctx, "read_thread", {"channel": "general", "ts": "nope"})).is_error
    unknown = await adapter.execute(ctx, "read_channel", {"channel": "does-not-exist"})
    assert (unknown.is_error, unknown.text) == (True, "No channel named #does-not-exist is known.")
    # The bot is not in #random (the policy refuses earlier; the adapter explains Slack's error).
    await bot_leaves("C0RANDOM")
    not_in = await adapter.execute(ctx, "read_channel", {"channel": "random"})
    assert not_in.is_error
    assert "/invite @agent" in not_in.text
    backwards = await adapter.execute(
        ctx,
        "read_channel",
        {"channel": "bugs", "start": "2026-10-02T00:00:00Z", "end": "2026-10-01T00:00:00Z"},
    )
    assert backwards.is_error
    assert "start is after end" in backwards.text


async def bot_leaves(channel: str) -> None:
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{FAKES}/slack/_ui/join",
            json={"team": "T0ACME", "channel": channel, "user": "U0ACMEBOT", "join": False},
        )


@needs_fakes
async def test_list_channels_shows_what_the_requester_may_read(stack: Stack) -> None:
    bob = await SlackAdapter().execute(tool_ctx(stack), "list_channels", {})
    alice = await SlackAdapter().execute(tool_ctx(stack, "alice"), "list_channels", {})
    assert "#bugs (public)" in bob.text
    assert "#leads-private" not in bob.text
    assert "#random" not in bob.text
    assert "#leads-private (private)" in alice.text
    assert "#old-project" not in alice.text  # archived


@needs_fakes
async def test_post_message_is_idempotent_by_key(stack: Stack) -> None:
    adapter = SlackAdapter()
    ctx = tool_ctx(stack)
    key = f"test:{uuid.uuid4().hex}"
    args = {
        "channel": "general",
        "text": "Daily summary with xoxb-123456789012-abcdefghij",
        "_idempotency_key": key,
    }
    first = await adapter.execute(ctx, "post_message", args)
    second = await adapter.execute(ctx, "post_message", args)
    assert first.data["ts"] == second.data["ts"]
    assert second.data.get("existing") is True
    async with httpx.AsyncClient() as client:
        posted = (
            await client.get(
                f"{FAKES}/_admin/slack/messages", params={"team": "T0ACME", "channel": "C0GENERAL"}
            )
        ).json()
    mine = [
        m for m in posted if (m.get("metadata") or {}).get("event_payload", {}).get("idempotency_key") == key
    ]
    assert len(mine) == 1
    assert "[redacted slack token]" in mine[0]["text"]

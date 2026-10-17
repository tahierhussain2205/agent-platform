"""The policy module: tool enablement, overrides, the channel rule, tiers by mode, credential modes,
the audience check, auditing of every decision, and tenant isolation."""

import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from app.policy import Destination, Policy, TaskScope
from app.tools.base import Operation, Source, Target, ToolContext, ToolResult
from app.tools.registry import Registry
from app.tools.slack import SlackAdapter
from tests.stack import Env, Stack


class TicketAdapter:
    """A stand-in tool with a reversible write, to exercise the Ask/Auto tiers."""

    key = "jira"

    def __init__(self) -> None:
        self.operations: dict[str, Operation] = {
            "create_issue": Operation(
                "create_issue",
                "Create an issue.",
                {"type": "object", "properties": {"summary": {"type": "string"}}},
                kind="write",
                credential_mode="user_required",
            ),
            "delete_issue": Operation(
                "delete_issue", "Delete.", {"type": "object"}, kind="write", reversible=False
            ),
            "search": Operation("search", "Search.", {"type": "object"}),
        }

    async def resolve_target(self, ctx: ToolContext, operation: str, args: dict[str, Any]) -> Target | None:
        return None

    async def execute(
        self, ctx: ToolContext, operation: str, args: dict[str, Any], credential: Any = None
    ) -> ToolResult:
        return ToolResult("ok")


def q(stack: Stack, statement: str, params: tuple[Any, ...] = ()) -> Any:
    with psycopg.connect(stack.env.world.url) as conn:
        cur = conn.execute(statement, params)
        row = cur.fetchone() if cur.description else None
        conn.commit()
        return row


@pytest.fixture(scope="module")
def channels(env: Env) -> dict[str, Target]:
    world = env.world
    with psycopg.connect(world.url) as conn:
        for key in ("slack", "jira"):
            conn.execute(
                "insert into app.org_integrations (org_id, integration_key, kind, enabled) values (%s, %s, 'native', true) "
                "on conflict (org_id, integration_key) do update set enabled = true",
                (world.acme.id, key),
            )
        # A private channel Alice and Bob are both in.
        row = conn.execute(
            "insert into app.channels (org_id, workspace_id, external_id, name, is_private, is_allowlisted, bot_is_member) "
            "values (%s, %s, 'C0EXECS', 'execs', true, true, true) returning id",
            (world.acme.id, world.acme.workspace),
        ).fetchone()
        assert row is not None
        for user in ("U0ALICE", "U0BOB"):
            conn.execute(
                "insert into app.channel_members (org_id, channel_id, external_user_id) values (%s, %s, %s)",
                (world.acme.id, row[0], user),
            )
        found = conn.execute(
            "select name, id, external_id, is_private from app.channels where org_id = %s", (world.acme.id,)
        ).fetchall()
        conn.commit()
    return {name: Target("slack_channel", cid, ext, name, private) for name, cid, ext, private in found}


def policy(stack: Stack, who: str = "bob", mode: Any = "ask", channel: str = "C0GENERAL") -> Policy:
    world = stack.env.world
    membership = world.acme.user if who == "bob" else world.acme.admin
    registry = Registry()
    registry.register(SlackAdapter())
    registry.register(TicketAdapter())
    task = q(
        stack,
        "insert into app.tasks (org_id, requested_by, trigger, model_tier) values (%s, %s, 'mention', 'standard') "
        "returning id",
        (world.acme.id, membership),
    )[0]
    scope = TaskScope(
        world.acme.id,
        task,
        membership,
        world.acme.workspace,
        "U0BOB" if who == "bob" else "U0ALICE",
        channel,
        None,
        mode,
    )
    return Policy(stack.pool, scope, registry)


async def test_channel_reads_follow_the_channel_rule(stack: Stack, channels: dict[str, Target]) -> None:
    bob, alice = policy(stack), policy(stack, "alice")
    allowed = await bob.check_tool("slack", "read_channel", channels["bugs"])
    assert (allowed.outcome, allowed.tier, allowed.credential_mode) == ("allowed", "read", "org_with_check")
    assert (await bob.check_tool("slack", "read_channel", channels["leads-private"])).code == "not_a_member"
    assert (await alice.check_tool("slack", "read_channel", channels["leads-private"])).allowed
    assert (
        await bob.check_tool("slack", "read_channel", channels["random"])
    ).code == "channel_not_allowlisted"
    assert (await bob.check_tool("slack", "read_thread", channels["old-project"])).code == "channel_archived"
    assert (await bob.check_tool("slack", "read_channel", None)).code == "channel_unknown"
    assert (await bob.check_tool("slack", "list_channels", None)).allowed  # no channel involved
    assert (await bob.check_tool("slack", "drop_tables", None)).code == "tool_unknown"
    assert (await bob.check_tool("github", "read_file", None)).code == "tool_unknown"


async def test_tool_must_be_enabled_and_not_turned_off(stack: Stack, channels: dict[str, Target]) -> None:
    acme = stack.env.world.acme
    bob = policy(stack)
    q(
        stack,
        "update app.org_integrations set enabled = false where org_id = %s and integration_key = 'slack'",
        (acme.id,),
    )
    try:
        assert (
            await bob.check_tool("slack", "read_channel", channels["bugs"])
        ).code == "integration_disabled"
    finally:
        q(
            stack,
            "update app.org_integrations set enabled = true where org_id = %s and integration_key = 'slack'",
            (acme.id,),
        )
    q(
        stack,
        "insert into app.operation_policies (org_id, integration_key, operation, credential_mode, enabled) "
        "values (%s, 'slack', 'read_channel', 'org_with_check', false)",
        (acme.id,),
    )
    try:
        assert (await bob.check_tool("slack", "read_channel", channels["bugs"])).code == "operation_disabled"
        assert (
            await bob.check_tool("slack", "read_thread", channels["bugs"])
        ).allowed  # other operations unaffected
    finally:
        q(stack, "delete from app.operation_policies where org_id = %s", (acme.id,))


async def test_credential_mode_overrides_and_user_links(stack: Stack, channels: dict[str, Target]) -> None:
    acme = stack.env.world.acme
    bob = policy(stack)
    q(
        stack,
        "insert into app.operation_policies (org_id, integration_key, operation, credential_mode) "
        "values (%s, 'slack', 'read_channel', 'user_required')",
        (acme.id,),
    )
    try:
        refused = await bob.check_tool("slack", "read_channel", channels["bugs"])
        assert (refused.code, refused.credential_mode) == ("link_required", "user_required")
        link = q(
            stack,
            "insert into app.connections (org_id, integration_key, scope, membership_id) values (%s, 'slack', 'user', %s) "
            "returning id",
            (acme.id, acme.user),
        )[0]
        linked = await bob.check_tool("slack", "read_channel", channels["bugs"])
        assert (linked.outcome, linked.credential_mode) == ("allowed", "user_required")
        q(stack, "delete from app.connections where id = %s", (link,))
    finally:
        q(stack, "delete from app.operation_policies where org_id = %s", (acme.id,))


async def test_action_tiers_against_the_mode(stack: Stack, channels: dict[str, Target]) -> None:
    acme = stack.env.world.acme
    ask, auto = policy(stack), policy(stack, mode="auto")
    # The create operation needs the requester's own linked account (the world fixture has one).
    linked = q(
        stack,
        "select count(*) from app.connections where org_id = %s and integration_key = 'jira' and scope = 'user' "
        "and membership_id = %s and status = 'active'",
        (acme.id, acme.user),
    )[0]
    assert linked == 1
    assert (await ask.check_tool("jira", "search", None)).outcome == "allowed"
    reversible_ask = await ask.check_tool("jira", "create_issue", None)
    assert (reversible_ask.outcome, reversible_ask.tier) == ("needs_approval", "reversible_write")
    assert reversible_ask.approver_rule == "requester_or_admin"
    assert (await auto.check_tool("jira", "create_issue", None)).outcome == "allowed"
    # Irreversible and outward facing actions need approval even in Auto.
    assert (await auto.check_tool("jira", "delete_issue", None)).outcome == "needs_approval"
    post = await auto.check_tool("slack", "post_message", channels["bugs"])
    assert (post.outcome, post.tier) == ("needs_approval", "irreversible")
    # Posting also follows the requester's access to the target channel.
    assert (await auto.check_tool("slack", "post_message", channels["random"])).outcome == "denied"
    assert (await auto.check_tool("slack", "post_message", channels["leads-private"])).code == "not_a_member"


async def test_roles_narrow_what_the_agent_does_and_route_approvals(
    stack: Stack, channels: dict[str, Target]
) -> None:
    """A role blocks operations for its holders, makes writes ask even in Auto, and an approver role
    takes over who decides (D-203, D-204). Bob holds "Support"; nobody has to hold "Tech Lead"."""
    acme = stack.env.world.acme
    support = q(
        stack,
        "insert into app.custom_roles (org_id, title, blocked_operations, ask_operations) "
        "values (%s, 'Support', '{jira.search}', '{jira.create_issue}') returning id",
        (acme.id,),
    )[0]
    q(
        stack,
        "insert into app.membership_roles (org_id, membership_id, role_id, is_primary) values (%s, %s, %s, "
        "not exists (select 1 from app.membership_roles where membership_id = %s))",
        (acme.id, acme.user, support, acme.user),
    )
    lead = q(
        stack,
        "insert into app.custom_roles (org_id, title, approves_operations) values (%s, 'Tech Lead', "
        "'{jira.create_issue,jira.delete_issue}') returning id",
        (acme.id,),
    )[0]
    try:
        bob_auto = policy(stack, mode="auto")
        blocked = await bob_auto.check_tool("jira", "search", None)
        assert (blocked.outcome, blocked.code) == ("denied", "role_blocked")
        assert blocked.reason == "Your role (Support) does not include jira search."
        asks = await bob_auto.check_tool("jira", "create_issue", None)
        assert (asks.outcome, asks.code) == ("needs_approval", "role_ask")
        assert asks.reason == "Your role (Support) asks before jira create_issue."
        assert (asks.approver_rule, asks.approver_role_id, asks.approver_role) == (
            "role_or_admin",
            lead,
            "Tech Lead",
        )
        irreversible = await bob_auto.check_tool("jira", "delete_issue", None)
        assert (irreversible.code, irreversible.approver_role) == ("approval_required", "Tech Lead")
        # Alice holds no role: nothing is blocked and Auto stays Auto; routing is the org's.
        alice_auto = policy(stack, who="alice", mode="auto")
        assert (await alice_auto.check_tool("jira", "search", None)).outcome == "allowed"
        routed = await alice_auto.check_tool("jira", "delete_issue", None)  # (create needs a linked account)
        assert (routed.outcome, routed.approver_rule, routed.approver_role) == (
            "needs_approval", "role_or_admin", "Tech Lead",
        )  # fmt: skip
        audited = q(
            stack,
            "select reason, metadata->>'code' from app.audit_log where task_id = %s and action = 'policy.tool' "
            "and target = 'jira.search'",
            (bob_auto.scope.task_id,),
        )
        assert audited == ("Your role (Support) does not include jira search.", "role_blocked")
    finally:
        q(stack, "delete from app.membership_roles where role_id = %s", (support,))
        q(stack, "update app.custom_roles set status = 'archived' where id in (%s, %s)", (support, lead))
    assert (await policy(stack, mode="auto").check_tool("jira", "search", None)).outcome == "allowed"


async def test_shadow_mode_reads_and_plans_but_never_writes(
    stack: Stack, channels: dict[str, Target]
) -> None:
    acme = stack.env.world.acme
    q(stack, "update app.org_settings set shadow_mode = true where org_id = %s", (acme.id,))
    try:
        for mode in ("ask", "auto"):
            p = policy(stack, mode=mode)
            assert (await p.check_tool("jira", "search", None)).outcome == "allowed"
            assert (await p.check_tool("slack", "read_channel", channels["bugs"])).outcome == "allowed"
            for adapter, operation, target in (("jira", "create_issue", None), ("jira", "delete_issue", None),
                                               ("slack", "post_message", channels["bugs"])):  # fmt: skip
                decision = await p.check_tool(adapter, operation, target)
                assert (decision.outcome, decision.code) == ("denied", "shadow_mode"), (mode, operation)
                assert decision.reason.startswith("Shadow mode:")
        # The channel rule still answers first for a channel the requester may not use.
        assert (
            await policy(stack).check_tool("slack", "post_message", channels["leads-private"])
        ).code == "not_a_member"
    finally:
        q(stack, "update app.org_settings set shadow_mode = false where org_id = %s", (acme.id,))
    assert (await policy(stack, mode="auto").check_tool("jira", "create_issue", None)).outcome == "allowed"


async def test_every_decision_is_audited(stack: Stack, channels: dict[str, Target]) -> None:
    bob = policy(stack)
    await bob.check_tool("slack", "read_channel", channels["bugs"])
    await bob.check_tool("slack", "read_channel", channels["leads-private"])
    await bob.check_tool("slack", "post_message", channels["general"])
    await bob.check_output(Destination("thread", "C0GENERAL", name="general"), [])
    with psycopg.connect(stack.env.world.url) as conn:
        rows = conn.execute(
            "select actor_type, actor_id, action, target, decision, metadata->>'code' from app.audit_log "
            "where task_id = %s order by occurred_at, id",
            (bob.scope.task_id,),
        ).fetchall()
    acme = stack.env.world.acme
    assert rows == [
        ("agent", acme.user, "policy.tool", "slack.read_channel #bugs", "allowed", "ok"),
        ("agent", acme.user, "policy.tool", "slack.read_channel #leads-private", "denied", "not_a_member"),
        ("agent", acme.user, "policy.tool", "slack.post_message #general", "info", "approval_required"),
        ("agent", acme.user, "policy.output", "thread general", "allowed", None),
    ]


def src(channel: str, name: str, visibility: Any = "public") -> Source:
    return Source(f"slack:{channel}:1.{uuid.uuid4().int % 1000}", channel, name, visibility, "1.1")


async def test_audience_check(stack: Stack, channels: dict[str, Target]) -> None:
    alice = policy(stack, "alice")
    general = Destination("thread", "C0GENERAL", name="general")
    leads = Destination("thread", "C0LEADS", name="leads-private")
    execs = Destination("thread", "C0EXECS", name="execs")
    assert (
        await alice.check_output(general, [src("C0GENERAL", "general"), src("C0BUGS", "bugs")])
    ).deliver == "as_is"
    leak = await alice.check_output(
        general, [src("C0BUGS", "bugs"), src("C0LEADS", "leads-private", "private")]
    )
    assert (leak.deliver, leak.restricted) == ("private", ("#leads-private",))
    # A private source into a private channel whose members can all read it.
    assert (await alice.check_output(leads, [src("C0EXECS", "execs", "private")])).deliver == "as_is"
    # ...but not the other way round: Bob is in #execs and not in #leads-private.
    assert (
        await alice.check_output(execs, [src("C0LEADS", "leads-private", "private")])
    ).deliver == "private"
    assert (await alice.check_output(leads, [src("C0LEADS", "leads-private", "private")])).deliver == "as_is"
    assert (await alice.check_output(general, [src("D0ALICE", "direct message", "dm")])).deliver == "private"
    assert (
        await alice.check_output(Destination("dm", "D0ALICE"), [src("C0LEADS", "leads", "private")])
    ).deliver == "as_is"
    unknown = Destination("thread", "C0UNTRACKED", name="untracked")
    assert (
        await alice.check_output(unknown, [src("C0LEADS", "leads-private", "private")])
    ).deliver == "private"


async def test_another_orgs_channels_are_invisible(stack: Stack, channels: dict[str, Target]) -> None:
    globex = stack.env.world.globex
    with psycopg.connect(stack.env.world.url) as conn:
        row = conn.execute(
            "select id, external_id, name from app.channels where org_id = %s and is_allowlisted limit 1",
            (globex.id,),
        ).fetchone()
    assert row is not None
    bob = policy(stack)
    decision = await bob.check_tool(
        "slack", "read_channel", Target("slack_channel", row[0], row[1], row[2], False)
    )
    assert decision.code == "channel_unknown"  # row level security hides it
    with psycopg.connect(stack.env.world.url) as conn:
        audited = conn.execute(
            "select org_id from app.audit_log where task_id = %s", (bob.scope.task_id,)
        ).fetchall()
    assert audited == [(stack.env.world.acme.id,)]


@pytest.fixture(autouse=True)
def _restore(stack: Stack) -> Iterator[None]:
    yield
    q(stack, "delete from app.operation_policies where org_id = %s", (stack.env.world.acme.id,))

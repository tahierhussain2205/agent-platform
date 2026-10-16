"""Integration adapters through the executor: credential modes, the requester's access (Jira project
permissions, GitHub repository access), MCP discovery and tiers, and the audience of restricted
sources. Runs against the Jira, GitHub and Google stand-ins through the vault."""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import pytest_asyncio

from app.policy import Destination, Policy, TaskScope
from app.tools.base import RefBook, Source, ToolContext
from app.tools.executor import ToolExecutor, build_registry
from tests.connect import (
    connect_custom_mcp,
    connect_github,
    connect_jira,
    enable,
    link_github,
    link_google,
    sql,
)
from tests.stack import FAKES, PROVIDER_SETTINGS, Stack, fakes_up

pytestmark = pytest.mark.skipif(not fakes_up(), reason="stand-ins not reachable (run `make infra`)")


@dataclass
class Setup:
    stack: Stack
    http: httpx.AsyncClient

    @property
    def acme(self) -> Any:
        return self.stack.env.world.acme

    async def executor(self, who: str = "bob", mode: Any = "ask", channel: str = "C0GENERAL") -> ToolExecutor:
        acme = self.acme
        member: uuid.UUID = (
            acme.user if who == "bob" else acme.admin if who == "alice" else uuid.UUID(str(who))
        )
        email = sql(self.stack, "select u.email from app.org_memberships m join app.users u on u.id = m.user_id "
                                "where m.id = %s", (member,))[0][0]  # fmt: skip
        task = sql(
            self.stack,
            "insert into app.tasks (org_id, requested_by, trigger, model_tier, status, started_at) "
            "values (%s, %s, 'mention', 'standard', 'running', now()) returning id",
            (acme.id, member),
        )[0][0]
        ctx = ToolContext(
            acme.id, member, acme.workspace, "T0ACME", "U0BOB", "xoxb-local-T0ACME", self.stack.pool,
            self.stack.slack, self.stack.kv, RefBook(), task_id=task, requester_email=email, http=self.http,
            settings=PROVIDER_SETTINGS, vault=self.stack.vault,
        )  # fmt: skip
        registry = await build_registry(ctx, "U0ACMEBOT")
        scope = TaskScope(acme.id, task, member, acme.workspace, "U0BOB", channel, None, mode)
        return ToolExecutor(ctx, registry, Policy(self.stack.pool, scope, registry))


@pytest_asyncio.fixture(loop_scope="session")
async def setup(stack: Stack) -> AsyncIterator[Setup]:
    acme = stack.env.world.acme
    for key, kind in (
        ("slack", "native"),
        ("jira", "native"),
        ("github", "native"),
        ("google_workspace", "vendor_mcp"),
    ):
        enable(stack, acme, key, kind)
    await connect_jira(stack, acme, "acc-alice")
    await connect_jira(stack, acme, "acc-bob", acme.user)
    await connect_github(stack, acme)
    await link_github(stack, acme, acme.user, "bob-gh")
    await link_google(stack, acme, acme.user, "bob@acme.test")
    async with httpx.AsyncClient(timeout=30) as http:
        yield Setup(stack, http)
    sql(stack, "delete from app.operation_policies where org_id = %s", (acme.id,))


# ------------------------------------------------------------------ Jira


async def test_jira_results_are_filtered_to_what_the_requester_can_browse(setup: Setup) -> None:
    bob = await setup.executor("bob")
    result = await bob.run("jira_search_issues", {"jql": "project in (PAY, SEC)"})
    assert result.decision is not None
    assert result.decision.credential_mode == "org_with_check"
    assert "PAY-101" in result.text
    assert "SEC-3" not in result.text  # the org connection (Alice's) sees it; Bob may not
    assert "1 more issue(s) are in projects you can't browse" in result.text
    denied = await bob.run("jira_get_issue", {"key": "SEC-3"})
    assert denied.is_error
    assert "can't browse" in denied.text
    issue = await bob.run("jira_get_issue", {"key": "pay-101"})
    assert "Description: Since this morning's deploy" in issue.text
    source = next(s for s in bob.ctx.refs.by_ref.values() if s.ref == "jira:PAY-101")
    assert (source.kind, source.visibility, source.audience) == ("jira", "restricted", "jira:PAY")
    assert source.permalink == "https://acme.atlassian.net/browse/PAY-101"
    alice = await setup.executor("alice")
    assert "SEC-3" in (await alice.run("jira_search_issues", {"project": "SEC"})).text


async def test_jira_finds_unlinked_requesters_by_email_and_refuses_strangers(setup: Setup) -> None:
    acme = setup.acme
    quinn = sql(setup.stack, "select m.id from app.org_memberships m join app.users u on u.id = m.user_id "
                             "where u.email = 'quinn@acme.test' and m.org_id = %s", (acme.id,))[0][0]  # fmt: skip
    as_quinn = await setup.executor(quinn)
    assert "WEB-7" in (await as_quinn.run("jira_search_issues", {"project": "WEB"})).text
    frank = sql(setup.stack, "select m.id from app.org_memberships m join app.users u on u.id = m.user_id "
                             "where u.email = 'frank@both.test' and m.org_id = %s", (acme.id,))[0][0]  # fmt: skip
    as_frank = await setup.executor(frank)
    refused = await as_frank.run("jira_search_issues", {"project": "PAY"})
    assert "PAY-101" not in refused.text
    assert "couldn't match you to a Jira account" in refused.text


async def test_user_required_override_uses_the_requesters_own_link(setup: Setup) -> None:
    acme = setup.acme
    sql(setup.stack, "insert into app.operation_policies (org_id, integration_key, operation, credential_mode) "
                     "values (%s, 'jira', 'search_issues', 'user_required') on conflict do nothing", (acme.id,))  # fmt: skip
    try:
        bob = await setup.executor("bob")
        result = await bob.run("jira_search_issues", {"jql": ""})
        assert result.decision is not None
        assert result.decision.credential_mode == "user_required"
        assert "SEC-3" not in result.text  # Jira applies Bob's own permissions
        alice = await setup.executor("alice")  # no Jira link of her own
        refused = await alice.run("jira_search_issues", {"jql": ""})
        assert refused.denied
        assert "Link your Jira account" in refused.text
    finally:
        sql(setup.stack, "delete from app.operation_policies where org_id = %s", (acme.id,))


# ------------------------------------------------------------------ GitHub


async def test_github_reads_check_the_requesters_repository_access(setup: Setup) -> None:
    bob = await setup.executor("bob")
    repos = await bob.run("github_list_repos", {})
    assert "acme/infra (private)" in repos.text  # names only, with the org connection
    assert (await bob.run("github_read_file", {"repo": "acme/infra", "path": "README.md"})).text.startswith(
        "Access denied: your GitHub account (bob-gh) can't read acme/infra"
    )
    readme = await bob.run("github_read_file", {"repo": "acme/cart-service", "path": "src/pricing/cart.py"})
    assert "assumes at most 19 items" in readme.text
    owners = await bob.run("github_get_codeowners", {"repo": "acme/web-app"})
    assert "* @quinn-gh" in owners.text  # found under .github/
    sources = {s.ref: s for s in bob.ctx.refs.by_ref.values()}
    assert sources["github:acme/cart-service:src/pricing/cart.py"].visibility == "restricted"
    assert sources["github:acme/web-app:.github/CODEOWNERS"].visibility == "public"
    alice = await setup.executor("alice")  # no GitHub link: the agent cannot check her access
    refused = await alice.run("github_read_file", {"repo": "acme/web-app", "path": "README.md"})
    assert "Link your GitHub account" in refused.text
    assert (
        await bob.run("github_read_file", {"repo": "acme/cart-service", "path": "../etc/passwd"})
    ).is_error


# ------------------------------------------------------------------ MCP


async def test_vendor_mcp_discovery_tiers_and_the_requesters_own_data(setup: Setup) -> None:
    bob = await setup.executor("bob")
    adapter = bob.registry.adapters["google_workspace"]
    credential = await bob.credential_for("google_workspace", "user_required")
    assert credential is not None
    await adapter.discover(bob.ctx, credential)  # type: ignore[attr-defined]
    tiers = {name: op.tier for name, op in adapter.operations.items()}
    assert tiers == {
        "sheets_read_range": "read",
        "drive_search": "read",
        "sheets_append_row": "reversible_write",
    }
    mine = await bob.run("google_workspace_sheets_read_range", {"spreadsheet_id": "bob-notes"})
    assert "Cart fix | In review" in mine.text
    theirs = await bob.run("google_workspace_sheets_read_range", {"spreadsheet_id": "bug-metrics"})
    assert theirs.is_error  # Alice's sheet, read with Bob's account
    append = await bob.run(
        "google_workspace_sheets_append_row", {"spreadsheet_id": "bob-notes", "values": ["x"]}
    )
    assert append.decision is not None
    assert append.decision.outcome == "needs_approval"  # a write in Ask mode, not performed
    calls = (await setup.http.get(f"{FAKES}/_admin/google/mcp-calls")).json()["google"]
    assert all(c["tool"] != "sheets_append_row" for c in calls)


async def test_custom_mcp_needs_approval_and_its_tools_are_tiered(setup: Setup) -> None:
    acme = setup.acme
    key = f"mcp_status_{uuid.uuid4().hex[:4]}"
    sql(setup.stack, "insert into app.org_integrations (org_id, integration_key, kind, enabled, config) "
                     "values (%s, %s, 'custom_mcp', false, '{}')", (acme.id, key))  # fmt: skip
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):  # enabling an unapproved custom server
        sql(setup.stack, "update app.org_integrations set enabled = true where org_id = %s and integration_key = %s",
            (acme.id, key))  # fmt: skip
    assert key not in (await setup.executor("bob")).registry.adapters
    await connect_custom_mcp(setup.stack, acme, key, f"{FAKES}/mcp/status", "mcp-key-local")
    bob = await setup.executor("bob")
    adapter = bob.registry.adapters[key]
    credential = await bob.credential_for(key, "org")
    assert credential is not None
    await adapter.discover(bob.ctx, credential)  # type: ignore[attr-defined]
    assert adapter.operations["restart_service"].tier == "irreversible"
    incidents = await bob.run(f"{key}_status_incidents", {})
    assert "INC-201" in incidents.text
    restart = await bob.run(f"{key}_restart_service", {"service": "cart"}, approved=False)
    assert restart.decision is not None
    assert restart.decision.outcome == "needs_approval"


# ------------------------------------------------------------------ the audience of restricted sources


async def test_restricted_sources_reach_a_channel_only_when_everyone_there_can_see_them(setup: Setup) -> None:
    alice = await setup.executor("alice", channel="C0BUGS")
    policy = alice.policy

    def source(kind: str, audience: str, visibility: Any = "restricted") -> Source:
        return Source(f"{audience}:x", "", audience, visibility, "", kind=kind, audience=audience)

    bugs = Destination("thread", "C0BUGS", name="bugs")
    general = Destination("thread", "C0GENERAL", name="general")
    pay, sec = source("jira", "jira:PAY"), source("jira", "jira:SEC")
    # #bugs: Alice, Bob and Quinn all have Jira accounts that can browse PAY, not SEC.
    assert (await policy.check_output(bugs, [pay], audience=alice.audience)).deliver == "as_is"
    assert (await policy.check_output(bugs, [sec], audience=alice.audience)).deliver == "private"
    # #general has a member who is not an org member (Mallory): restricted sources stay private.
    assert (await policy.check_output(general, [pay], audience=alice.audience)).deliver == "private"
    # Without an audience check, restricted sources are never posted.
    assert (await policy.check_output(bugs, [pay])).deliver == "private"
    public_repo = source("github", "github:acme/web-app", "public")
    assert (await policy.check_output(general, [public_repo], audience=alice.audience)).deliver == "as_is"
    private_repo = source("github", "github:acme/cart-service")
    assert (await policy.check_output(bugs, [private_repo], audience=alice.audience)).deliver == "private"


async def test_disabled_integrations_are_not_loaded(setup: Setup) -> None:
    acme = setup.acme
    enable(setup.stack, acme, "github", enabled=False)
    try:
        bob = await setup.executor("bob")
        assert "github" not in bob.registry.adapters
        assert (await bob.run("github_list_repos", {})).text.startswith(
            "The tool github_list_repos is not available"
        )
    finally:
        enable(setup.stack, acme, "github")

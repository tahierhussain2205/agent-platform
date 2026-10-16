"""Jira writes through the executor: the requester's own linked account acts (issues carry their
name), Ask needs approval while Auto proceeds, retries never duplicate (the label or reference a
write carries is looked up first), assignees resolve by email, and components are listed within the
requester's access."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from app.tools.base import definitions
from app.tools.jira import JiraAdapter, marker
from tests.connect import connect_jira, consent, delete_jira_issues, enable, sql
from tests.stack import FAKES, PROVIDER_SETTINGS, Stack, fakes_up
from tests.tools.test_integrations import Setup

pytestmark = pytest.mark.skipif(not fakes_up(), reason="stand-ins not reachable (run `make infra`)")
JIRA = f"{FAKES}/atlassian/api/ex/jira/cloud-acme/rest/api/3"
RUN = uuid.uuid4().hex[:8]  # the stand-in keeps issues between runs


@pytest_asyncio.fixture(loop_scope="session")
async def setup(stack: Stack) -> AsyncIterator[Setup]:
    acme = stack.env.world.acme
    for key in ("slack", "jira"):
        enable(stack, acme, key)
    await connect_jira(stack, acme, "acc-alice")
    await connect_jira(stack, acme, "acc-bob", acme.user)  # Bob's own link; Alice has none
    async with httpx.AsyncClient(timeout=30) as http:
        yield Setup(stack, http)
        # The stand-in keeps issues: leave none of ours open (other tests look for duplicates).
        await delete_jira_issues(RUN)


async def as_account(account: str, path: str, method: str = "GET", **kw: Any) -> Any:
    """Straight to the Jira stand-in, as ``account``."""
    code = (await consent("/atlassian/authorize", account, redirect_uri="http://x/cb", state="s", scope="r"))[
        "code"
    ]
    async with httpx.AsyncClient() as client:
        token = (
            await client.post(
                f"{FAKES}/atlassian/oauth/token",
                json={"grant_type": "authorization_code", "code": code, "redirect_uri": "http://x/cb",
                      "client_id": "local-jira-client", "client_secret": "local-jira-client-secret"},
            )
        ).json()["access_token"]  # fmt: skip
        return (
            await client.request(method, f"{JIRA}/{path}", headers={"authorization": f"Bearer {token}"}, **kw)
        ).json()


async def as_quinn(path: str, method: str = "GET", **kw: Any) -> Any:
    """As someone else who can browse PAY."""
    return await as_account("acc-quinn", path, method, **kw)


def ticket(**overrides: Any) -> dict[str, Any]:
    return {
        "project": "PAY",
        "summary": f"Checkout fails for carts over 20 items {RUN}",
        "description": "Reported in #bugs: https://acme.slack.test/archives/C0BUGS/p1",
        "assignee_email": "quinn@acme.test",
        "component": "Checkout",
        **overrides,
    }


async def test_writes_are_reversible_user_required_and_not_offered_when_internal() -> None:
    ops = JiraAdapter("http://x").operations
    assert {n: (o.tier, o.credential_mode) for n, o in ops.items() if o.kind == "write"} == {
        "create_issue": ("reversible_write", "user_required"),
        "assign_issue": ("reversible_write", "user_required"),
        "add_comment": ("reversible_write", "user_required"),
    }
    names = [d["name"] for d in definitions(JiraAdapter("http://x"), list(ops))]
    assert "jira_match_accounts" not in names
    assert "jira_report_issues" not in names
    assert "jira_create_issue" in names


async def test_an_issue_is_created_with_the_requesters_account(setup: Setup) -> None:
    bob = await setup.executor("bob", mode="auto")
    result = await bob.run("jira_create_issue", {**ticket(), "_idempotency_key": f"task:t1-{RUN}:action:1"})
    assert not result.is_error, result.text
    assert result.decision is not None
    assert result.decision.credential_mode == "user_required"
    key = result.data["key"]
    assert result.data["target_ref"] == f"jira:{key}"
    assert result.data["url"] == f"https://acme.atlassian.net/browse/{key}"
    fields = (await as_quinn(f"issue/{key}"))["fields"]
    assert fields["reporter"]["accountId"] == "acc-bob"  # under the requester's name
    assert fields["assignee"]["accountId"] == "acc-quinn"  # resolved from the email
    assert fields["components"] == [{"name": "Checkout"}]
    assert marker(f"task:t1-{RUN}:action:1") in fields["labels"]
    assert "Reported in #bugs" in fields["description"]["content"][0]["content"][0]["text"]


async def test_a_retry_finds_the_issue_instead_of_creating_another(setup: Setup) -> None:
    bob = await setup.executor("bob", mode="auto")
    args = {
        **ticket(summary=f"Refund emails go out twice {RUN}"),
        "_idempotency_key": f"task:t2-{RUN}:action:1",
    }
    first = await bob.run("jira_create_issue", args)
    again = await bob.run("jira_create_issue", args)  # the answer to the first call was lost
    assert again.data["key"] == first.data["key"]
    assert "exists already" in again.text
    found = await as_quinn(
        "search/jql", "POST", json={"jql": f'labels = "{marker(f"task:t2-{RUN}:action:1")}"'}
    )
    assert len(found["issues"]) == 1


async def test_ask_needs_approval_and_auto_proceeds(setup: Setup) -> None:
    asked = await (await setup.executor("bob", mode="ask")).run("jira_create_issue", ticket())
    assert asked.decision is not None
    assert asked.decision.outcome == "needs_approval"
    assert asked.decision.approver_rule == "requester_or_admin"
    assert not asked.data  # nothing was created
    auto = await (await setup.executor("bob", mode="auto")).run(
        "jira_create_issue", ticket(summary=f"Auto ticket {RUN}")
    )
    assert auto.decision is not None
    assert auto.decision.outcome == "allowed"


async def test_the_issue_type_is_one_the_project_has(setup: Setup) -> None:
    """Staging: a team-managed project (a new site's first) has Task but no Bug, and Jira refused the
    ticket. SEC in the stand-in is such a project; only Alice browses it, so she links for this test."""
    stack, acme = setup.stack, setup.acme
    link = await connect_jira(stack, acme, "acc-alice", acme.admin)
    try:
        alice = await setup.executor("alice", mode="auto")
        bug = {**ticket(project="SEC", summary=f"Rotate the webhook key {RUN}"), "component": "",
               "assignee_email": ""}  # fmt: skip
        created = await alice.run("jira_create_issue", {**bug, "_idempotency_key": f"task:t9-{RUN}:action:1"})
        assert not created.is_error, created.text
        assert (await as_account("acc-alice", f"issue/{created.data['key']}"))["fields"]["issuetype"] == {
            "name": "Task"
        }
        bob = await setup.executor("bob", mode="auto")
        story = {**ticket(summary=f"Show saved carts {RUN}"), "issue_type": "story"}
        created = await bob.run(
            "jira_create_issue", {**story, "_idempotency_key": f"task:t10-{RUN}:action:1"}
        )
        assert not created.is_error, created.text
        assert (await as_quinn(f"issue/{created.data['key']}"))["fields"]["issuetype"] == {"name": "Story"}
    finally:
        sql(stack, "update app.connections set status = 'revoked' where id = %s", (link,))


async def test_without_a_linked_account_there_is_no_write(setup: Setup) -> None:
    alice = await setup.executor("alice", mode="auto")  # the org connection is hers, but no link
    result = await alice.run("jira_create_issue", ticket(), approved=True)
    assert result.denied
    assert "Link your Jira account" in result.text


async def test_assignees_resolve_by_email_or_the_write_is_refused(setup: Setup) -> None:
    bob = await setup.executor("bob", mode="auto")
    refused = await bob.run("jira_create_issue", ticket(assignee_email="nobody@acme.test"))
    assert refused.is_error
    assert "no Jira user has the email nobody@acme.test" in refused.text
    unassigned = await bob.run(
        "jira_create_issue", ticket(summary=f"Nobody owns this {RUN}", assignee_email="")
    )
    assert not unassigned.is_error
    key = unassigned.data["key"]
    assert (await as_quinn(f"issue/{key}"))["fields"]["assignee"] is None
    assigned = await bob.run("jira_assign_issue", {"key": key, "assignee_email": "alice@acme.test"})
    assert not assigned.is_error, assigned.text
    assert (await as_quinn(f"issue/{key}"))["fields"]["assignee"]["accountId"] == "acc-alice"


async def test_a_comment_is_posted_once(setup: Setup) -> None:
    bob = await setup.executor("bob", mode="auto")
    args = {
        "key": "PAY-102",
        "text": "Seen again today in #bugs",
        "_idempotency_key": f"task:t3-{RUN}:action:1",
    }
    first = await bob.run("jira_add_comment", args)
    again = await bob.run("jira_add_comment", args)
    assert not first.is_error
    assert "already" in again.text
    comments = (await as_quinn("issue/PAY-102/comment"))["comments"]
    ours = [c for c in comments if marker(f"task:t3-{RUN}:action:1") in str(c["body"])]
    assert len(ours) == 1
    assert ours[0]["author"]["accountId"] == "acc-bob"


async def test_components_within_the_requesters_access(setup: Setup) -> None:
    bob = await setup.executor("bob")
    listed = await bob.run("jira_list_components", {"project": "PAY"})
    assert {c["name"] for c in listed.data["components"]} == {"Checkout", "Refunds", "Webhooks"}
    assert "Checkout (lead Bob Builder)" in listed.text
    denied = await bob.run("jira_list_components", {"project": "SEC"})
    assert denied.is_error
    assert "can't browse the SEC project" in denied.text


async def test_match_accounts_is_for_code_only(setup: Setup) -> None:
    alice = await setup.executor("alice")
    offered = {"jira_search_issues"}
    assert (await alice.run("jira_match_accounts", {"emails": ["bob@acme.test"]}, allowed=offered)).is_error
    found = await alice.run("jira_match_accounts", {"emails": ["bob@acme.test", "nobody@acme.test"]})
    assert found.data["accounts"] == {"bob@acme.test": "acc-bob"}
    audited = sql(setup.stack, "select count(*) from app.audit_log where target = 'jira.match_accounts'")[0][
        0
    ]
    assert audited >= 1
    assert PROVIDER_SETTINGS.atlassian_api_url

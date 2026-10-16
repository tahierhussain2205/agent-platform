"""Example 1 (spec 3.2): problems reported in a channel become Jira tickets under the requester's
account, assigned to service catalog owners. Code drops duplicates of open issues, checks assignees
against the catalog and links the Slack messages; Ask asks first, Auto proceeds (dropping to Ask for a
large plan or a private source), and writes are never duplicated on retry. The runner's steps are
tested directly, then the whole task through the workflow (approval requests appear together; links
are posted in the thread).

Each test reads a channel of its own. The Jira stand-in keeps issues between runs (an open one would
be a duplicate): the seed's issues are put back first and every test deletes what it created."""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest_asyncio

from app.agent.types import PlannedAction, TaskRef, Understanding
from app.slack.adapter import ApprovalRequest, Reply
from app.tools.jira import marker
from tests.agent.conftest import Agent, channel_messages, ui_post
from tests.agent.live import Live, thread_of, wait_for
from tests.connect import connect_jira, delete_jira_issues, enable, reset_jira_issues
from tests.stack import FAKES
from tests.tools.test_jira_writes import as_quinn

PHRASES = [
    "Discount codes fail at checkout",
    "Refund emails are sent twice",
    "Search filters are broken on mobile",
    "Invoice PDF",
]


async def delete_ours() -> None:
    await delete_jira_issues(*PHRASES)


def member(agent: Agent, email: str) -> Any:
    return agent.sql(
        "select m.id from app.org_memberships m join app.users u on u.id = m.user_id "
        "where m.org_id = %s and u.email = %s",
        (agent.world.acme.id, email),
    )[0][0]


def set_mode(agent: Agent, mode: str) -> None:
    acme = agent.world.acme
    agent.sql(
        "insert into app.user_preferences (membership_id, org_id, mode) values (%s, %s, %s) "
        "on conflict (membership_id) do update set mode = excluded.mode",
        (acme.user, acme.id, mode),
    )


@pytest_asyncio.fixture(loop_scope="session")
async def tickets(agent: Agent) -> AsyncIterator[Agent]:
    stack, acme = agent.stack, agent.world.acme
    enable(stack, acme, "jira")
    await connect_jira(stack, acme, "acc-alice")
    await connect_jira(stack, acme, "acc-bob", acme.user)  # Bob files tickets with his own account
    agent.sql(
        "delete from app.services where org_id = %s and name in ('cart-service', 'Refunds', 'web-app')",
        (acme.id,),
    )
    for name, project, component, owner, reason in (
        ("cart-service", "PAY", "Checkout", acme.user, "CODEOWNERS /src/pricing/ in acme/cart-service"),
        ("Refunds", "PAY", "Refunds", acme.admin, "lead of Jira component PAY/Refunds"),
        ("web-app", "WEB", "Search", member(agent, "quinn@acme.test"), "lead of Jira component WEB/Search"),
    ):
        service = agent.sql(
            "insert into app.services (org_id, name, jira_project, jira_component) values (%s, %s, %s, %s) "
            "returning id",
            (acme.id, name, project, component),
        )[0][0]
        agent.sql(
            "insert into app.service_owners (org_id, service_id, membership_id, source, reason) "
            "values (%s, %s, %s, 'codeowners', %s)",
            (acme.id, service, owner, reason),
        )
    reset_jira_issues()  # PAY-101 open, as seeded: the duplicate the tests expect
    yield agent
    set_mode(agent, "ask")
    agent.sql(
        "update app.tasks set status = 'completed', finished_at = now() where org_id = %s "
        "and status in ('queued', 'running', 'awaiting_approval')",
        (acme.id,),
    )


async def channel(agent: Agent, private: bool = False) -> tuple[str, str]:
    """A fresh allowlisted channel with Bob, Quinn, Alice and the bot: (name, Slack id)."""
    name = f"tickets-{uuid.uuid4().hex[:6]}"
    members = ["U0BOB", "U0QUINN", "U0ALICE", "U0ACMEBOT"]
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{FAKES}/_admin/slack/channels",
            json={"team": "T0ACME", "name": name, "is_private": private, "members": members},
        )
    external = r.json()["id"]
    acme = agent.world.acme
    workspace = agent.sql("select id from app.workspaces where external_id = 'T0ACME'")[0][0]
    channel_id = agent.sql(
        "insert into app.channels (org_id, workspace_id, external_id, name, is_private, is_allowlisted, "
        "bot_is_member) values (%s, %s, %s, %s, %s, true, true) returning id",
        (acme.id, workspace, external, name, private),
    )[0][0]
    for user in members[:3]:
        agent.sql(
            "insert into app.channel_members (org_id, channel_id, external_user_id) values (%s, %s, %s)",
            (acme.id, channel_id, user),
        )
    return name, external


async def ask(agent: Agent, text: str, user: str = "U0BOB") -> TaskRef:
    message = await ui_post(user, "C0GENERAL", text)
    ref = await agent.task(user, "C0GENERAL", ts=message["ts"])
    start = await agent.runner.start(ref)
    assert start.status == "started", start
    return ref


async def planned(
    agent: Agent, request: str, mode: str = "ask"
) -> tuple[TaskRef, Understanding, list[PlannedAction]]:
    ref = await ask(agent, request)
    u = await agent.runner.understand(ref, mode)
    assert u.playbook_key == "jira_tickets", u
    f = await agent.runner.gather(ref, u, "standard")
    return ref, u, await agent.runner.plan(ref, u, f, "standard")


def by_summary(actions: list[PlannedAction], summary: str) -> PlannedAction:
    return next(a for a in actions if a.args.get("summary") == summary)


async def issue(key: str) -> dict[str, Any]:
    fields: dict[str, Any] = (await as_quinn(f"issue/{key}"))["fields"]
    return fields


def text_of(adf: Any) -> str:
    if isinstance(adf, dict):
        if adf.get("type") == "text":
            return str(adf["text"])
        if adf.get("type") == "hardBreak":
            return "\n"
        return "".join(text_of(c) for c in adf.get("content") or []) + (
            "\n\n" if adf.get("type") == "paragraph" else ""
        )
    return ""


# ------------------------------------------------------------------ the plan, checked in code


async def test_duplicates_assignees_and_links_are_checked_in_code(tickets: Agent) -> None:
    agent = tickets
    name, external = await channel(agent)
    await ui_post("U0QUINN", external, "Checkout fails with a 500 for carts over 20 items again")
    await ui_post("U0QUINN", external, "Discount codes fail at checkout")
    await ui_post("U0ALICE", external, "Refund emails are sent twice")
    await ui_post("U0BOB", external, "Refund emails go out twice for every refund")
    ref, u, actions = await planned(agent, f"create Jira tickets for the problems reported today in #{name}")
    assert len(actions) == 3  # the two refund reports are one problem

    duplicate = actions[0]
    assert duplicate.skip is not None
    assert "looks like <https://acme.atlassian.net/browse/PAY-101|PAY-101> (In Progress), already open" in (
        duplicate.skip
    )

    discount = by_summary(actions, "Discount codes fail at checkout")
    assert (discount.skip, discount.ask) == (None, False)
    assert discount.summary == 'Create PAY ticket "Discount codes fail at checkout" for Bob Builder'
    assert {k: discount.args[k] for k in ("project", "component", "assignee_email")} == {
        "project": "PAY",
        "component": "Checkout",
        "assignee_email": "bob@acme.test",
    }
    assert discount.evidence == [
        "Assignee: Bob Builder (bob@acme.test), owner of cart-service (CODEOWNERS /src/pricing/ in acme/cart-service)"
    ]
    description = discount.args["description"]
    assert f"/ui#T0ACME/{external}/" in description  # the Slack message it comes from
    assert description.endswith("Assigned by the agent to Bob Builder: owner of cart-service (CODEOWNERS "
                                "/src/pricing/ in acme/cart-service).")  # fmt: skip
    assert "finding" not in discount.args

    refunds = by_summary(actions, "Refund emails are sent twice")
    assert refunds.args["assignee_email"] == "alice@acme.test"
    assert refunds.args["description"].count(f"/ui#T0ACME/{external}/") == 2  # both reports

    # Ask: an approval request that shows the ticket, the assignee and why.
    mark = len(agent.sink.sent)
    outcome = await agent.runner.act(ref, u, discount, 2, "", [])
    assert outcome.status == "awaiting_approval"
    (request,) = [i for i, _ in agent.sink.sent[mark:] if isinstance(i, ApprovalRequest)]
    assert request.summary.startswith(
        'Create PAY ticket "Discount codes fail at checkout" for Bob Builder\n'
        "Project PAY, component Checkout\n"
        "Assignee: Bob Builder (bob@acme.test), owner of cart-service (CODEOWNERS /src/pricing/ in acme/cart-service)\n"
        "> Reported in Slack by Quinn Tester: Discount codes fail at checkout"
    )
    assert request.tier == "reversible_write"
    assert (await agent.runner.act(ref, u, duplicate, 1, "", [])).status == "skipped"

    # Approved: created once, under Bob's account, assigned to the owner.
    agent.sql(
        "update app.approvals set status = 'approved', decided_by = %s where id = %s",
        (agent.world.acme.user, outcome.approval_id),
    )
    done = await agent.runner.execute(ref, u, discount, 2, "")
    assert done.status == "done", done
    key = done.result["key"]
    assert (
        done.message
        == f"Created <https://acme.atlassian.net/browse/{key}|{key}>: Discount codes fail at checkout"
    )
    fields = await issue(key)
    assert (fields["reporter"]["accountId"], fields["assignee"]["accountId"]) == ("acc-bob", "acc-bob")
    assert fields["components"] == [{"name": "Checkout"}]
    links = [
        m
        for c in fields["description"]["content"]
        for n in c.get("content") or []
        for m in n.get("marks") or []
    ]
    assert any(f"/ui#T0ACME/{external}/" in m["attrs"]["href"] for m in links)  # a link in Jira too
    assert "Assigned by the agent to Bob Builder" in text_of(fields["description"])

    # Retries never create a second issue: recorded as done, or found by its label when the
    # answer from Jira was lost before we recorded it.
    again = await agent.runner.execute(ref, u, discount, 2, "")
    assert (again.status, again.result) == ("done", {"target": f"jira:{key}"})
    agent.sql(
        "update app.external_actions set status = 'pending', target_ref = null where idempotency_key = %s",
        (f"task:{ref.task_id}:action:2",),
    )
    recovered = await agent.runner.execute(ref, u, discount, 2, "")
    assert recovered.result["key"] == key
    found = await as_quinn(
        "search/jql", "POST", json={"jql": f'labels = "{marker(f"task:{ref.task_id}:action:2")}"'}
    )
    assert [i["key"] for i in found["issues"]] == [key]
    assert agent.sql(
        "select status, target_ref from app.external_actions where idempotency_key = %s",
        (f"task:{ref.task_id}:action:2",),
    ) == [("succeeded", f"jira:{key}")]
    await delete_ours()


async def test_an_issue_filed_while_waiting_for_approval_is_not_duplicated(tickets: Agent) -> None:
    agent = tickets
    ref = await ask(agent, "create Jira tickets for the problems in this thread")
    u = Understanding("jira_tickets", 1, 0.9, None, "ask", brief={"goal": "tickets"})
    args = {"project": "PAY", "summary": "Invoice PDFs fail to download", "description": "From #bugs",
            "assignee_email": "", "component": ""}  # fmt: skip
    action = PlannedAction("jira", "create_issue", args, 'Create PAY ticket "Invoice PDFs fail to download"')
    filed = await as_quinn(
        "issue",
        "POST",
        json={
            "fields": {
                "project": {"key": "PAY"},
                "summary": "Invoice PDF downloads fail",
                "issuetype": {"name": "Bug"},
            }
        },
    )
    outcome = await agent.runner.execute(ref, u, action, 1, "")
    assert outcome.status == "skipped"
    assert (
        f"looks like <https://acme.atlassian.net/browse/{filed['key']}|{filed['key']}>, opened meanwhile"
        in (outcome.message)
    )
    assert not agent.sql(
        "select 1 from app.external_actions where idempotency_key = %s", (f"task:{ref.task_id}:action:1",)
    )
    await delete_ours()


async def test_auto_drops_to_ask_for_a_plan_above_three_changes(tickets: Agent) -> None:
    agent = tickets
    name, external = await channel(agent)
    for user, text in (
        ("U0QUINN", "Discount codes fail at checkout"),
        ("U0ALICE", "Refund emails are sent twice"),
        ("U0QUINN", "Search filters are broken on mobile"),
        ("U0BOB", "Invoice PDFs fail to download"),
    ):
        await ui_post(user, external, text)
    mark = len(agent.sink.sent)
    ref, u, actions = await planned(
        agent, f"create PAY tickets for the problems reported today in #{name}", "auto"
    )
    assert u.mode == "auto"
    assert [a.skip for a in actions] == [None] * 4
    assert all(a.ask for a in actions)
    notes = [i.text for i, _ in agent.sink.sent[mark:] if isinstance(i, Reply)]
    assert "This plan makes 4 changes, more than 3, so I'll ask before each one." in notes
    search = by_summary(actions, "Search filters are broken on mobile")
    assert search.args["assignee_email"] == "quinn@acme.test"  # named after the WEB Search component
    assert search.args["component"] == ""  # that component is not in PAY
    invoice = by_summary(actions, "Invoice PDFs fail to download")
    assert invoice.args["assignee_email"] == ""
    assert invoice.evidence[-1].startswith("Unassigned: the service catalog suggests more than one owner")
    assert invoice.summary.endswith("(unassigned)")
    outcome = await agent.runner.act(ref, u, actions[0], 1, "", [])
    assert outcome.status == "awaiting_approval"  # Auto, but asked


async def test_auto_proceeds_within_the_ceiling(tickets: Agent) -> None:
    agent = tickets
    name, external = await channel(agent)
    await ui_post("U0QUINN", external, "Discount codes fail at checkout")
    ref, u, actions = await planned(
        agent, f"create Jira tickets for the problems reported today in #{name}", "auto"
    )
    (action,) = actions
    assert not action.ask
    outcome = await agent.runner.act(ref, u, action, 1, "", [])
    assert outcome.status == "done", outcome
    assert (await issue(outcome.result["key"]))["reporter"]["accountId"] == "acc-bob"
    await delete_ours()


async def test_a_private_conversation_always_asks_and_says_so(tickets: Agent) -> None:
    agent = tickets
    name, external = await channel(agent, private=True)
    await ui_post("U0QUINN", external, "Discount codes fail at checkout")
    ref, u, actions = await planned(
        agent, f"create Jira tickets for the problems reported today in #{name}", "auto"
    )
    (action,) = actions
    assert action.ask
    assert action.evidence[-1] == (
        f"⚠️ From a private conversation (#{name}): everyone who can browse PAY will see this ticket"
    )
    mark = len(agent.sink.sent)
    assert (await agent.runner.act(ref, u, action, 1, "", [])).status == "awaiting_approval"
    (request,) = [i for i, _ in agent.sink.sent[mark:] if isinstance(i, ApprovalRequest)]
    assert "From a private conversation" in request.summary


async def test_an_assignee_needs_evidence(tickets: Agent) -> None:
    agent = tickets
    name, external = await channel(agent)
    await ui_post("U0QUINN", external, "Discount codes fail at checkout")
    ref = await ask(agent, f"create Jira tickets for the problems reported today in #{name}")
    u = await agent.runner.understand(ref, "ask")
    f = await agent.runner.gather(ref, u, "standard")
    from tests.agent.conftest import script_model

    proposal = {"project": "PAY", "summary": "Discount codes fail at checkout", "description": "From Slack",
                "assignee_email": "quinn@acme.test", "finding": 1}  # fmt: skip
    named = {**proposal, "summary": "Discount codes are not applied", "assignee_email": "alice@acme.test"}
    await script_model(
        [{"type": "tool_use", "name": "submit_plan", "input": {"actions": [
            {"tool": "jira", "operation": "create_issue", "args": proposal, "summary": "Ticket"},
            {"tool": "jira", "operation": "create_issue", "args": named, "summary": "Ticket"},
            {"tool": "jira", "operation": "assign_issue", "args": {"key": "PAY-104", "assignee_email": "quinn@acme.test"},
             "summary": "Assign PAY-104 to Quinn"},
        ]}}]
    )  # fmt: skip
    actions = await agent.runner.plan(ref, u, f, "standard")
    # Quinn owns nothing that matches: the catalog's owner instead, and the approver is told.
    assert actions[0].args["assignee_email"] == "bob@acme.test"
    assert (
        actions[0].evidence[0]
        == "Not assigned to quinn@acme.test: the service catalog has no evidence they own this"
    )
    # The same problem twice in one plan is one ticket.
    assert actions[1].skip is not None
    assert "same problem" in actions[1].skip
    # Assigning an existing issue needs the requester to name the person.
    assert actions[2].skip == (
        "Not assigning PAY-104 to quinn@acme.test: the service catalog has no evidence they own this"
    )


# ------------------------------------------------------------------ through the task workflow

JQL = "project = PAY AND statusCategory != Done AND reporter = acc-bob ORDER BY created DESC"


def approvals(agent: Agent, event_id: str) -> list[tuple[str, str, str]]:
    rows = agent.sql(
        "select a.id::text, a.message_ref, a.action_summary from app.approvals a join app.tasks t "
        "on t.id = a.task_id where t.workflow_id = %s and a.message_ref is not null order by a.created_at",
        (f"task-{event_id}",),
    )
    return [(r[0], r[1].rsplit(":", 1)[1], r[2]) for r in rows or []]


async def open_by_bob(summary: str) -> list[dict[str, Any]]:
    found = await as_quinn(
        "search/jql", "POST", json={"jql": JQL, "fields": ["summary", "assignee", "reporter"]}
    )
    return [i for i in found["issues"] if i["fields"]["summary"] == summary]


def answer(messages: list[dict[str, Any]], ts: str) -> str:
    replies = [m for m in thread_of(messages, ts) if not m.get("edited") and "Created" in m.get("text", "")]
    return json.dumps(replies[-1]["blocks"]) if replies else ""


async def test_ask_requests_approvals_together_and_creates_what_is_approved(
    tickets: Agent, live: Live
) -> None:
    name, external = await channel(tickets)
    await ui_post("U0QUINN", external, "Checkout fails with a 500 for carts over 20 items again")
    await ui_post("U0QUINN", external, "Discount codes fail at checkout")
    await ui_post("U0ALICE", external, "Refund emails are sent twice")
    # Asked where the problems were reported: everyone here may see PAY, so the answer stays here.
    event_id, ts = await live.request(
        f"create Jira tickets for the problems reported today in #{name}", channel=external
    )
    pending = await wait_for(
        lambda: len(approvals(tickets, event_id)) == 2 and approvals(tickets, event_id), 90
    )
    discount = next(a for a in pending if "Discount codes" in a[2])
    refunds = next(a for a in pending if "Refund emails" in a[2])
    assert "Assignee: Bob Builder (bob@acme.test), owner of cart-service" in discount[2]

    async def waiting() -> bool:
        status = [m for m in thread_of(await channel_messages(external), ts) if m.get("edited")]
        return bool(status) and status[0]["text"] == "⏸ Waiting for 2 approvals"

    assert await wait_for(waiting)

    assert (await live.click("U0BOB", "approval_reject", refunds[0], ts=refunds[1])).status_code == 200
    assert (await live.click("U0BOB", "approval_approve", discount[0], ts=discount[1])).status_code == 200
    assert await live.result(event_id) == "completed"

    (created,) = await open_by_bob("Discount codes fail at checkout")
    assert created["fields"]["assignee"]["accountId"] == "acc-bob"
    assert not await open_by_bob("Refund emails are sent twice")
    final = answer(await channel_messages(external), ts)
    key = created["key"]
    assert (
        f"Created <https://acme.atlassian.net/browse/{key}|{key}>: Discount codes fail at checkout" in final
    )
    assert "looks like <https://acme.atlassian.net/browse/PAY-101|PAY-101>" in final
    assert 'Refund emails are sent twice\\" for Alice Admin: not done (rejected)' in final
    await delete_ours()


async def test_auto_creates_the_tickets_and_posts_the_links(tickets: Agent, live: Live) -> None:
    set_mode(tickets, "auto")
    try:
        name, external = await channel(tickets)
        await ui_post("U0QUINN", external, "Discount codes fail at checkout")
        await ui_post("U0ALICE", external, "Refund emails are sent twice")
        event_id, ts = await live.request(
            f"create Jira tickets for the problems reported today in #{name}", channel=external
        )
        assert await live.result(event_id) == "completed"
        assert not approvals(tickets, event_id)
        (discount,) = await open_by_bob("Discount codes fail at checkout")
        (refunds,) = await open_by_bob("Refund emails are sent twice")
        assert refunds["fields"]["assignee"]["accountId"] == "acc-alice"
        final = answer(await channel_messages(external), ts)
        for issue in (discount, refunds):
            assert f"<https://acme.atlassian.net/browse/{issue['key']}|{issue['key']}>" in final
        done = tickets.sql(
            "select count(*) from app.external_actions a join app.tasks t on t.id = a.task_id "
            "where t.workflow_id = %s and a.status = 'succeeded' and a.target_ref like 'jira:PAY-%%'",
            (f"task-{event_id}",),
        )
        assert done == [(2,)]
    finally:
        set_mode(tickets, "ask")
        await delete_ours()

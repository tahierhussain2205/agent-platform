"""Example 2, part 1: an issue becomes a sourced plan. The case file is found in code (issue,
catalog service, window); the logs, code and changes investigators run as isolated subagents with
their own tools; the plan is checked in code (only cited evidence, investigate an unproven
hypothesis first, reproduce first, confidence capped by independent evidence); a follow-up in the
thread revises it. Runs against the Jira, GitHub, Google Cloud, sandbox and model stand-ins."""

import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from temporalio.api.enums.v1 import EventType

from app.agent.investigation import TOOLS, check_plan, facts_of, render, window_of
from app.agent.types import Findings, TaskRef
from app.tools.executor import ExecResult
from tests.agent.conftest import Agent, channel_messages, ui_post
from tests.agent.live import Live, thread_of, wait_for
from tests.connect import (
    connect_github,
    connect_jira,
    connect_logging,
    enable,
    link_github,
    reset_jira_issues,
)
from tests.stack import FAKES, Stack

LOG_SOURCE = {
    "provider": "google_cloud_logging",
    "project": "acme-prod",
    "resource_type": "cloud_run_revision",
}


# ------------------------------------------------------------------ the plan, checked in code


def plan_with(**overrides: Any) -> dict[str, Any]:
    return {
        "summary": "Carts of 20 or more items fail.", "goal": "Fix checkout", "confidence": "high",
        "evidence": [{"text": "IndexError x37", "sources": [1]}, {"text": "MAX_LINES = 19", "sources": [2]}],
        "hypotheses": [{"text": "The preallocated list overflows", "confidence": "high", "evidence": [1, 2]}],
        "steps": [{"kind": "implement", "text": "Grow the list"}, {"kind": "verify", "text": "Run tests"}],
        "files": [{"repo": "acme/cart-service", "path": "src/pricing/cart.py", "why": "the bug"}],
        **overrides,
    }  # fmt: skip


def test_unsourced_evidence_is_dropped_and_listed() -> None:
    plan, not_verified = check_plan(plan_with(evidence=[{"text": "A", "sources": [1]}, {"text": "B", "sources": []},
                                                        {"text": "C", "sources": [99]}]), 2, {1: "logs", 2: "code"},
                                    {"acme/cart-service:src/pricing/cart.py"})  # fmt: skip
    assert [e["text"] for e in plan["evidence"]] == ["A"]
    assert not_verified == ["Evidence without a source: B", "Evidence without a source: C"]


def test_reproduce_first_and_investigate_an_unproven_cause_first() -> None:
    plan, _ = check_plan(plan_with(), 2, {1: "logs", 2: "code"}, set())
    assert [s["kind"] for s in plan["steps"]] == ["reproduce", "implement", "verify"]
    unproven = plan_with(hypotheses=[{"text": "Maybe the tax service", "confidence": "high", "evidence": []}])
    plan, _ = check_plan(unproven, 2, {1: "logs", 2: "code"}, set())
    assert plan["hypotheses"][0]["confidence"] == "low"
    assert plan["steps"][0] == {
        "text": "Confirm or rule out first: Maybe the tax service",
        "kind": "investigate",
    }
    assert plan["confidence"] == "low"


def test_confidence_needs_independent_evidence() -> None:
    same_kind = plan_with(hypotheses=[{"text": "X", "confidence": "high", "evidence": [1, 2]}])
    plan, _ = check_plan(same_kind, 2, {1: "logs", 2: "logs"}, set())
    assert plan["hypotheses"][0]["confidence"] == "medium"
    assert plan["confidence"] == "medium"
    plan, _ = check_plan(plan_with(), 2, {1: "logs", 2: "code"}, set())
    assert plan["confidence"] == "high"
    ranked, _ = check_plan(plan_with(hypotheses=[{"text": "weak", "confidence": "low", "evidence": [1]},
                                                 {"text": "strong", "confidence": "medium", "evidence": [1, 2]}]),
                           2, {1: "logs", 2: "code"}, set())  # fmt: skip
    assert [h["text"] for h in ranked["hypotheses"]] == ["strong", "weak"]


def test_files_not_read_are_flagged_and_text_is_escaped() -> None:
    plan, not_verified = check_plan(
        plan_with(files=[{"repo": "acme/web-app", "path": "/search/index.ts"}], summary="<!channel> look"),
        2, {1: "logs", 2: "code"}, set(),
    )  # fmt: skip
    assert plan["files"] == [{"repo": "acme/web-app", "path": "search/index.ts", "why": "", "read": False}]
    assert not_verified == ["acme/web-app search/index.ts was not read in this investigation"]
    text = render(plan)
    assert "&lt;!channel>" in text
    assert "<!channel>" not in text
    assert "_(not read yet)_" in text
    assert "[1]" in text


def test_windows_come_from_the_brief_the_issue_or_the_last_day() -> None:
    now = datetime(2026, 10, 16, 12, tzinfo=UTC)
    assert window_of({"start": "2026-10-16T06:00:00+00:00", "end": None}, None, now) == [
        "2026-10-16T06:00:00+00:00", now.isoformat(),
    ]  # fmt: skip
    issue = {"created": "2026-10-16T05:00:00.000+0000", "status": "In Progress"}
    assert window_of(None, issue, now)[0] == "2026-10-16T03:00:00+00:00"  # two hours before
    done = {
        "created": "2026-10-14T10:00:00.000+0000",
        "status": "Done",
        "updated": "2026-10-15T09:00:00.000+0000",
    }
    assert window_of(None, done, now) == ["2026-10-14T08:00:00+00:00", "2026-10-15T10:00:00+00:00"]
    assert window_of(None, None, now) == [(now - timedelta(days=1)).isoformat(), now.isoformat()]
    old = {"created": "2026-09-01T00:00:00.000+0000", "status": "To Do"}
    assert window_of(None, old, now)[0] == (now - timedelta(days=7)).isoformat()  # at most a week


def test_facts_come_from_tool_data() -> None:
    logs = ExecResult("...", data={"groups": [{"signature": "IndexError: x @ a.py:1 in f", "count": 3}]})
    code = ExecResult("...", data={"repo": "acme/r", "path": "a.py", "start": 1, "end": 9})
    denied = ExecResult("Access denied", True, denied=True, data={"groups": [{"signature": "secret"}]})
    facts = facts_of(
        "logs", [("x_search_events", logs), ("github_read_code", code), ("x_search_events", denied)]
    )
    assert [s["signature"] for s in facts["signatures"]] == ["IndexError: x @ a.py:1 in f"]
    assert facts["files"] == [{"repo": "acme/r", "path": "a.py", "lines": [1, 9]}]
    assert set(TOOLS) == {"logs", "code", "changes"}


# ------------------------------------------------------------------ against the stand-ins


@pytest_asyncio.fixture(loop_scope="session")
async def world(agent: Agent) -> AsyncIterator[Agent]:
    stack: Stack = agent.stack
    acme = stack.env.world.acme
    for key in ("jira", "github"):
        enable(stack, acme, key)
    await connect_jira(stack, acme, "acc-alice")
    await connect_github(stack, acme)
    await link_github(stack, acme, acme.user, "bob-gh")
    await connect_logging(stack, acme)
    reset_jira_issues()
    agent.sql(
        "delete from app.services where org_id = %s and name in ('cart-service', 'web-app')", (acme.id,)
    )
    for name, repo, project, component in (("cart-service", "acme/cart-service", "PAY", "Checkout"),
                                           ("web-app", "acme/web-app", "WEB", "Search")):  # fmt: skip
        repo_id = agent.sql(
            "insert into app.repositories (org_id, external_id, full_name) values (%s, %s, %s) "
            "on conflict (org_id, provider, external_id) do update set full_name = excluded.full_name returning id",
            (acme.id, f"R_{name}", repo),
        )[0][0]
        agent.sql(
            "insert into app.services (org_id, name, repository_id, jira_project, jira_component, log_source) "
            "values (%s, %s, %s, %s, %s, %s)",
            (acme.id, name, repo_id, project, component, json.dumps(LOG_SOURCE)),
        )
    yield agent


@pytest.fixture(autouse=True)
def _close_tasks(agent: Agent) -> Iterator[None]:
    yield
    agent.sql("update app.tasks set status = 'completed', finished_at = now() where org_id = %s "
              "and status in ('queued', 'running', 'awaiting_approval')", (agent.world.acme.id,))  # fmt: skip


async def ask(agent: Agent, text: str, thread_ts: str | None = None) -> TaskRef:
    message = await ui_post("U0BOB", "C0GENERAL", text, thread_ts)
    ref = await agent.task("U0BOB", "C0GENERAL", ts=message["ts"], thread_ts=thread_ts)
    assert (await agent.runner.start(ref)).status == "started"
    return ref


async def investigate(agent: Agent, ref: TaskRef) -> tuple[Any, list[Findings], Any]:
    u = await agent.runner.understand(ref, "ask")
    assert u.playbook_key == "investigate_issue", u
    case = await agent.runner.orient(ref, u)
    found = [
        await agent.runner.investigate(ref, u, case, kind, "standard") for kind in ("logs", "code", "changes")
    ]
    composed = await agent.runner.plan_investigation(ref, u, case, found, "standard")
    return case, found, composed


def stored_plan(agent: Agent, ref: TaskRef) -> dict[str, Any]:
    rows = agent.sql("select content from app.artifacts where task_id = %s and kind = 'plan'", (ref.task_id,))
    assert rows
    plan: dict[str, Any] = rows[0][0]
    return plan


async def test_a_ticket_becomes_a_sourced_plan(world: Agent) -> None:
    ref = await ask(world, "investigate PAY-101 and plan a fix")
    case, found, composed = await investigate(world, ref)
    assert case.issue is not None
    assert case.issue["key"] == "PAY-101"
    assert case.service is not None
    assert (case.service["name"], case.service["repository"], case.service["logs"]) == (
        "cart-service", "acme/cart-service", True,
    )  # fmt: skip
    start = datetime.fromisoformat(case.window[0])
    assert timedelta(hours=7) < datetime.now(UTC) - start < timedelta(hours=9)  # two hours before the issue
    logs, code, changes = found
    assert (logs.kind, code.kind, changes.kind) == ("logs", "code", "changes")
    # Facts are what the tools returned.
    assert logs.facts["signatures"][0]["top_frame"] == "src/pricing/cart.py:7 in total"
    assert {"repo": "acme/cart-service", "path": "src/pricing/cart.py", "lines": [1, 8]} in code.facts[
        "files"
    ]
    assert any(c["message"] == "Preallocate price slots for faster totals" for c in changes.facts["commits"])
    # Each investigator saw only its own tools.
    plan = stored_plan(world, ref)
    assert plan["playbook"] == "investigate_issue"
    assert plan["confidence"] == "high"
    top = plan["hypotheses"][0]
    assert "Preallocate price slots for faster totals" in top["text"]
    assert "MAX_LINES = 19" in top["text"]
    assert plan["steps"][0]["kind"] == "reproduce"
    assert plan["files"] == [{"repo": "acme/cart-service", "path": "src/pricing/cart.py", "why": "where the problem is",
                              "read": True}]  # fmt: skip
    kinds = {s.get("kind") for s in composed.sources}
    assert {"logs", "github"} <= kinds
    assert composed.answer.startswith(
        "*Plan for <https://acme.atlassian.net/browse/PAY-101|PAY-101>: Checkout returns 500 for carts with 20 or "
        "more items*"
    )
    assert "[1]" in composed.answer
    assert composed.not_verified == []


async def test_a_follow_up_in_the_thread_revises_the_plan(world: Agent) -> None:
    first = await ask(world, "investigate PAY-101")
    await investigate(world, first)
    await world.runner.finish(first, "completed")  # one task at a time per thread
    follow = await ask(world, "it only happens for carts with discounts", thread_ts=first.message_ts)
    assert follow.session_id == first.session_id
    case, _, composed = await investigate(world, follow)
    assert case.prior_plan is not None
    assert case.prior_plan["playbook"] == "investigate_issue"
    assert case.issue is not None
    assert case.issue["key"] == "PAY-101"  # from the current plan: the follow-up names no issue
    plan = stored_plan(world, follow)
    assert plan["revision"]["changed"] is True
    assert "_Revised: " in composed.answer
    assert plan["summary"].startswith("On your follow-up")


async def test_a_plan_for_these_tickets_covers_the_ones_made_in_the_thread(world: Agent) -> None:
    """The demo: tickets made in a thread, then "a detailed plan to address these tickets" there. The
    agent does not read its own messages back: the tickets come from what earlier tasks created."""
    first = await ask(world, "create Jira tickets for these issues")
    for n, key in enumerate(("PAY-102", "PAY-104"), 1):
        world.sql(
            "insert into app.external_actions (org_id, task_id, integration_key, operation, target_ref, "
            "idempotency_key, status, performed_at) values (%s, %s, 'jira', 'create_issue', %s, %s, 'succeeded', "
            "now() + %s * interval '1 second')",
            (world.world.acme.id, first.task_id, f"jira:{key}", f"task:{first.task_id}:action:{n}", n),
        )
    await world.runner.finish(first, "completed")
    follow = await ask(
        world, "come up with a detailed plan to address these tickets", thread_ts=first.message_ts
    )
    case, _, composed = await investigate(world, follow)
    assert [i["key"] for i in case.issues] == ["PAY-102", "PAY-104"]
    assert case.issue is not None
    assert case.issue["key"] == "PAY-102"
    plan = stored_plan(world, follow)
    assert [i["key"] for i in plan["case"]["issues"]] == ["PAY-102", "PAY-104"]
    assert [i["key"] for i in plan["issues"]] == ["PAY-102", "PAY-104"]
    assert composed.answer.startswith(
        "*Plan for <https://acme.atlassian.net/browse/PAY-102|PAY-102>, "
        "<https://acme.atlassian.net/browse/PAY-104|PAY-104>: "
    )
    # Another thread's tickets are not this thread's.
    other = await ask(world, "come up with a detailed plan to address these tickets")
    assert await world.runner._session_tickets(other) == []


async def test_an_empty_plan_is_asked_for_again_and_never_posted_empty(world: Agent) -> None:
    """Demo run 4: the plan came back with nothing in it, and "Plan for KAN-11, KAN-12:" was posted
    with no goal, evidence or steps."""
    from tests.agent.conftest import script_model

    ref = await ask(world, "investigate PAY-101 and plan a fix")
    u = await world.runner.understand(ref, "ask")
    case = await world.runner.orient(ref, u)
    found = [await world.runner.investigate(ref, u, case, k, "standard") for k in ("logs", "code", "changes")]
    await script_model([{"type": "tool_use", "name": "submit_investigation_plan", "input": {"notes": "x"}}])
    composed = await world.runner.plan_investigation(ref, u, case, found, "standard")
    assert composed.answer.startswith("*Plan for ")  # the second answer was a plan
    assert "Write a failing test" in composed.answer
    await world.runner.finish(ref, "completed")

    again = await ask(world, "investigate PAY-101 and plan a fix")
    u2 = await world.runner.understand(again, "ask")
    case2 = await world.runner.orient(again, u2)
    found2 = [
        await world.runner.investigate(again, u2, case2, k, "standard") for k in ("logs", "code", "changes")
    ]
    empty = {"type": "tool_use", "name": "submit_investigation_plan", "input": {}}
    await script_model([empty], [empty])
    composed2 = await world.runner.plan_investigation(again, u2, case2, found2, "standard")
    assert composed2.answer == (
        "I couldn't write a plan this time: the answer I got had no plan in it. Ask me again."
    )
    assert world.sql(
        "select count(*) from app.artifacts where task_id = %s and kind = 'plan'", (again.task_id,)
    ) == [(0,)]


async def test_what_could_not_be_read_is_said_and_lowers_confidence(world: Agent) -> None:
    acme = world.world.acme
    world.sql("update app.services set log_source = '{}' where org_id = %s and name = 'web-app'", (acme.id,))
    try:
        ref = await ask(world, "why is search slow in the EU? see WEB-7")
        case, found, composed = await investigate(world, ref)
        assert "web-app has no log source in the catalog" in case.notes
        assert any("no log source" in g for g in found[0].gaps)
        plan = stored_plan(world, ref)
        assert plan["confidence"] != "high"  # code and changes only
        assert any(n.startswith("Not covered: web-app has no log source") for n in composed.not_verified)
    finally:
        world.sql("update app.services set log_source = %s where org_id = %s and name = 'web-app'",
                  (json.dumps(LOG_SOURCE), acme.id))  # fmt: skip


async def test_the_workflow_runs_the_investigators_in_parallel(world: Agent, live: Live) -> None:
    event_id, ts = await live.request("investigate PAY-101")
    assert await live.result(event_id) == "completed"
    handle = live.client.get_workflow_handle(f"task-{event_id}")
    scheduled: list[int] = []
    completed: list[int] = []
    async for event in handle.fetch_history_events():
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            if event.activity_task_scheduled_event_attributes.activity_type.name == "agent_investigate":
                scheduled.append(event.event_id)
        elif event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED and scheduled:
            completed.append(event.event_id)
    assert len(scheduled) == 3
    assert max(scheduled) < completed[0]  # all three were scheduled before any finished

    # The plan cites a private repository and the org's logs, and not everyone in #general may see
    # them: it goes to Bob privately, with a note in the thread.
    async def noted() -> Any:
        return [
            m for m in thread_of(await channel_messages("C0GENERAL"), ts) if "privately" in m.get("text", "")
        ]

    assert await wait_for(noted)
    async with httpx.AsyncClient() as client:
        opened = await client.post(f"{FAKES}/slack/api/conversations.open", data={"users": "U0BOB"},
                                   headers={"authorization": "Bearer xoxb-local-T0ACME"})  # fmt: skip
    dm = opened.json()["channel"]["id"]
    plans = [
        m
        for m in await channel_messages(dm)
        if "*Plan for <https://acme.atlassian.net/browse/PAY-101|PAY-101>: Checkout returns 500"
        in json.dumps(m)
    ]
    assert plans
    assert "IndexError" in json.dumps(plans[-1])


async def test_a_past_incidents_code_is_read_as_it_was_then(world: Agent) -> None:
    acme = world.world.acme
    world.sql("insert into app.services (org_id, name, repository_id, jira_project, jira_component, log_source) "
              "select org_id, 'webhooks', repository_id, 'PAY', 'Webhooks', %s from app.services "
              "where org_id = %s and name = 'cart-service' on conflict do nothing",
              (json.dumps({**LOG_SOURCE, "value": "cart-service"}), acme.id))  # fmt: skip
    try:
        ref = await ask(world, "investigate PAY-105: how did the webhook signature failures happen?")
        case, found, _ = await investigate(world, ref)
        assert case.code_at == case.window[0]  # resolved: the window ended a day ago
        (base,) = world.sql("select base_sha from app.sandboxes where task_id = %s", (ref.task_id,))[0]
        head = [c for c in found[2].facts.get("commits") or [] if c.get("message")]
        before = next(c for c in head if c["message"] == "Verify webhook signatures with the signing key")
        assert base == before["sha"]  # the commit before the incident, not the fix after it
        assert "read once at import" in stored_plan(world, ref)["hypotheses"][0]["text"]
    finally:
        world.sql("delete from app.services where org_id = %s and name = 'webhooks'", (acme.id,))

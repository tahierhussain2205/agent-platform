"""Example 2, part 2: the thread's plan becomes a pull request. Prepared in code (the plan, the
repository, the base, a branch under agent/, a working copy), implemented test first by an isolated
subagent with sandbox tools, verified in code (the tests, and the new test failing without the fix),
reviewed by a subagent that sees only the plan and the diff, exported and checked, then pushed as an
action through the policy. Review comments come back to the thread through GitHub's webhook, and
"address the review comments" revises the same pull request. Runs against the stand-ins: GitHub,
the sandbox provider and its runner, Slack and the model."""

import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from temporalio.api.enums.v1 import EventType

from app.agent.types import TaskRef, Understanding
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

REPO = "acme/cart-service"
LOG_SOURCE = {
    "provider": "google_cloud_logging",
    "project": "acme-prod",
    "resource_type": "cloud_run_revision",
}
WEBHOOK_SECRET = "test-github-webhook-secret"


@pytest_asyncio.fixture(loop_scope="session")
async def world(agent: Agent) -> AsyncIterator[Agent]:
    stack: Stack = agent.stack
    acme = stack.env.world.acme
    for key in ("jira", "github"):
        enable(stack, acme, key)
    await connect_jira(stack, acme, "acc-alice")
    await connect_github(stack, acme)
    await link_github(stack, acme, acme.user, "bob-gh")  # Bob can push to cart-service
    await link_github(stack, acme, acme.admin, "quinn-gh")  # read only
    await connect_logging(stack, acme)
    reset_jira_issues()
    for name, repo, project, component in (("cart-service", "acme/cart-service", "PAY", "Checkout"),
                                           ("web-app", "acme/web-app", "WEB", "Search")):  # fmt: skip
        repo_id = agent.sql(
            "insert into app.repositories (org_id, external_id, full_name) values (%s, %s, %s) "
            "on conflict (org_id, provider, external_id) do update set full_name = excluded.full_name returning id",
            (acme.id, f"R_{name}", repo),
        )[0][0]
        agent.sql(
            "insert into app.services (org_id, name, repository_id, jira_project, jira_component, log_source) "
            "values (%s, %s, %s, %s, %s, %s) on conflict do nothing",
            (acme.id, name, repo_id, project, component, json.dumps(LOG_SOURCE)),
        )
    yield agent


@pytest.fixture(autouse=True)
def _close_tasks(agent: Agent) -> Iterator[None]:
    yield
    agent.sql("update app.tasks set status = 'completed', finished_at = now() where org_id = %s "
              "and status in ('queued', 'running', 'awaiting_approval')", (agent.world.acme.id,))  # fmt: skip


def set_auto(agent: Agent, on: bool) -> None:
    acme = agent.world.acme
    mode = "auto" if on else "ask"
    agent.sql("insert into app.user_preferences (membership_id, org_id, mode) values (%s, %s, %s) "
              "on conflict (membership_id) do update set mode = excluded.mode", (acme.user, acme.id, mode))  # fmt: skip
    agent.sql("delete from app.autonomy_policies where org_id = %s and scope_type = 'org'", (acme.id,))
    if on:
        agent.sql("insert into app.autonomy_policies (org_id, scope_type, max_mode) values (%s, 'org', 'auto')",
                  (acme.id,))  # fmt: skip


async def own_channel(agent: Agent) -> str:
    """A private channel with Bob and the bot only: everyone there can read the repository."""
    name = f"change-{uuid.uuid4().hex[:6]}"
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{FAKES}/_admin/slack/channels",
                              json={"team": "T0ACME", "name": name, "is_private": True,
                                    "members": ["U0BOB", "U0ACMEBOT"]})  # fmt: skip
    external: str = r.json()["id"]
    acme = agent.world.acme
    workspace = agent.sql("select id from app.workspaces where external_id = 'T0ACME'")[0][0]
    channel_id = agent.sql(
        "insert into app.channels (org_id, workspace_id, external_id, name, is_private, is_allowlisted, "
        "bot_is_member) values (%s, %s, %s, %s, true, true, true) returning id",
        (acme.id, workspace, external, name),
    )[0][0]
    agent.sql("insert into app.channel_members (org_id, channel_id, external_user_id) values (%s, %s, 'U0BOB')",
              (acme.id, channel_id))  # fmt: skip
    return external


async def ask(agent: Agent, text: str, thread_ts: str | None = None, channel: str = "C0GENERAL",
              user: str = "U0BOB") -> TaskRef:  # fmt: skip
    message = await ui_post(user, channel, text, thread_ts)
    ref = await agent.task(user, channel, ts=message["ts"], thread_ts=thread_ts,
                           channel_type="group" if channel != "C0GENERAL" else "channel")  # fmt: skip
    assert (await agent.runner.start(ref)).status == "started"
    return ref


def plan_for(
    path: str, repo: str = REPO, issue: str | None = "PAY-101", goal: str = "Fix checkout"
) -> dict[str, Any]:
    """A current plan as the investigation stores it."""
    return {
        "playbook": "investigate_issue", "version": 1, "goal": goal, "summary": f"The cause is in {path}.",
        "confidence": "high", "evidence": [], "hypotheses": [{"text": f"A bug in {path}", "confidence": "high",
                                                              "evidence": [1]}],
        "steps": [{"kind": "reproduce", "text": "Write a failing test"}, {"kind": "implement", "text": "Fix it"},
                  {"kind": "verify", "text": "Run the tests"}],
        "files": [{"repo": repo, "path": path, "why": "where the problem is", "read": True}],
        "verification": "The errors stop in the logs.", "case": {"issue": {"key": issue} if issue else None},
    }  # fmt: skip


async def thread_with_plan(agent: Agent, plan: dict[str, Any], channel: str = "C0GENERAL") -> TaskRef:
    first = await ask(agent, "investigate the checkout failures", channel=channel)
    agent.sql("insert into app.artifacts (org_id, task_id, kind, content) values (%s, %s, 'plan', %s)",
              (agent.world.acme.id, first.task_id, json.dumps(plan)))  # fmt: skip
    await agent.runner.finish(first, "completed")
    return first


async def understood(agent: Agent, ref: TaskRef, mode: str = "ask") -> Understanding:
    u = await agent.runner.understand(ref, mode)
    assert u.playbook_key == "implement_change", u
    return u


def github_state(repo: str = REPO) -> dict[str, Any]:
    state: dict[str, Any] = httpx.get(f"{FAKES}/_admin/github/repos/{repo}").json()
    return state


def approve(agent: Agent, approval_id: str) -> None:
    agent.sql("update app.approvals set status = 'approved', decided_at = now(), decided_by = %s where id = %s",
              (agent.world.acme.user, approval_id))  # fmt: skip


# ------------------------------------------------------------------ the steps


async def test_a_plan_becomes_a_pull_request(world: Agent) -> None:
    # Example 2 from the start: the investigation's plan, then "implement the plan" in its thread.
    first = await ask(world, "investigate PAY-101 and plan a fix")
    u0 = await world.runner.understand(first, "ask")
    case0 = await world.runner.orient(first, u0)
    found = [
        await world.runner.investigate(first, u0, case0, k, "standard") for k in ("logs", "code", "changes")
    ]
    await world.runner.plan_investigation(first, u0, case0, found, "standard")
    await world.runner.finish(first, "completed")
    ref = await ask(world, "looks right, go ahead and implement the plan", thread_ts=first.message_ts)
    u = await understood(world, ref)
    case = await world.runner.prepare_change(ref, u)
    assert case.stopped is None, case.stopped
    main = github_state()["refs"]["main"]
    assert (case.repo, case.base_sha, case.issue_key) == (REPO, main, "PAY-101")
    assert case.branch.startswith("agent/pay-101-checkout-returns-500")
    assert case.recipe == "python -m pytest -q -p no:cacheprovider"
    assert case.plan["files"][0]["path"] == "src/pricing/cart.py"
    implemented = await world.runner.implement(ref, u, case, "standard", {})
    assert "sums the price of every item" in implemented.summary
    assert implemented.tests == ["tests/test_cart_many_items.py"]
    verification = await world.runner.verify_change(ref, u, case)
    assert (verification.ok, verification.ran, verification.reproduced) == (True, True, True)
    review = await world.runner.review_change(ref, u, case, "standard")
    assert review.approved, review
    exported = await world.runner.export_change(ref, u, case, verification, review, implemented)
    assert exported.blocked == []
    assert exported.ask == []
    assert sorted(f["path"] for f in exported.files) == [
        "src/pricing/cart.py",
        "tests/test_cart_many_items.py",
    ]
    assert world.sql("select status from app.sandboxes where task_id = %s", (ref.task_id,)) == [
        ("destroyed",)
    ]
    (stored,) = world.sql(
        "select content from app.artifacts where task_id = %s and kind = 'diff'", (ref.task_id,)
    )
    assert stored[0]["base_sha"] == main
    assert stored[0]["verification"]["reproduced"] is True
    action = exported.action
    assert action is not None
    assert action.args["branch"] == case.branch
    # The reviewer saw the plan and the diff, never the implementer's conversation.
    reviews = [
        r for r in await model_requests() if (r.get("tool_choice") or {}).get("name") == "submit_review"
    ]
    reviewer = json.dumps(reviews[-1]["messages"])
    assert "<diff>" in reviewer
    assert "sandbox_run_command" not in reviewer
    assert implemented.summary not in reviewer
    # Ask mode: the push waits for a person.
    outcome = await world.runner.act(ref, u, action, 1, "", [])
    assert outcome.status == "awaiting_approval"
    assert case.branch not in github_state()["refs"]
    approval = world.sql(
        "select action_summary, action_tier from app.approvals where id = %s", (outcome.approval_id,)
    )
    assert approval[0][1] == "reversible_write"
    assert "2 files" in approval[0][0]
    assert "Verified: All tests pass." in approval[0][0]
    approve(world, str(outcome.approval_id))
    assert (await world.runner.resolve_approval(ref, str(outcome.approval_id))).status == "approved"
    done = await world.runner.execute(ref, u, action, 1, "")
    assert done.status == "done", done
    number = done.result["number"]
    state = github_state()
    pr = next(p for p in state["pulls"] if p["number"] == number)
    assert pr["head"]["ref"] == case.branch
    assert pr["title"].startswith("PAY-101: ")
    assert "### Verified" in pr["body"]
    assert "The new test fails without the fix and passes with it." in pr["body"]
    assert world.sql("select external_id from app.task_links where task_id = %s", (ref.task_id,)) == [
        (f"{REPO}#{number}",)
    ]
    composed = await world.runner.report_change(
        ref, u, case, [implemented], verification, review, exported, done
    )
    assert composed.answer.startswith(f"*Opened <{pr['html_url']}|{REPO}#{number}>* on `{case.branch}`.")
    assert "*Tests*: All tests pass." in composed.answer
    assert "The new test fails without the fix and passes with it." in composed.answer
    assert "*Review*: approved." in composed.answer
    assert composed.not_verified == []


async def model_requests() -> list[dict[str, Any]]:
    async with httpx.AsyncClient() as client:
        data: list[dict[str, Any]] = (await client.get(f"{FAKES}/_admin/anthropic/requests")).json()
    return data


async def test_auto_pushes_at_once_unless_the_checks_ask(world: Agent) -> None:
    set_auto(world, True)
    try:
        first = await thread_with_plan(world, plan_for("src/pricing/cart.py"))
        ref = await ask(world, "go ahead and fix it", thread_ts=first.message_ts)
        u = await understood(world, ref, "auto")
        case = await world.runner.prepare_change(ref, u)
        implemented = await world.runner.implement(ref, u, case, "standard", {})
        verification = await world.runner.verify_change(ref, u, case)
        review = await world.runner.review_change(ref, u, case, "standard")
        exported = await world.runner.export_change(ref, u, case, verification, review, implemented)
        assert exported.action is not None
        assert not exported.action.ask
        outcome = await world.runner.act(ref, u, exported.action, 1, "", [])
        assert outcome.status == "done", outcome  # Auto, within the ceiling: no approval
        assert case.branch in github_state()["refs"]
        # A protected path asks even in Auto.
        world.sql(
            "update app.repositories set protected_paths = '{src/pricing/}' where full_name = %s", (REPO,)
        )
        second = await thread_with_plan(world, plan_for("src/pricing/cart.py"))
        ref2 = await ask(world, "go ahead and fix it", thread_ts=second.message_ts)
        u2 = await understood(world, ref2, "auto")
        case2 = await world.runner.prepare_change(ref2, u2)
        impl2 = await world.runner.implement(ref2, u2, case2, "standard", {})
        ver2 = await world.runner.verify_change(ref2, u2, case2)
        exported2 = await world.runner.export_change(ref2, u2, case2, ver2, None, impl2)
        assert "src/pricing/cart.py is a protected path." in exported2.ask
        assert exported2.action is not None
        assert exported2.action.ask
        asked = await world.runner.act(ref2, u2, exported2.action, 1, "", [])
        assert asked.status == "awaiting_approval"
    finally:
        set_auto(world, False)
        world.sql("update app.repositories set protected_paths = '{}' where full_name = %s", (REPO,))


async def test_tests_that_cannot_run_are_reported_and_need_a_person(world: Agent) -> None:
    first = await thread_with_plan(world, plan_for("search/index.ts", repo="acme/web-app", issue="WEB-7",
                                                   goal="EU searches are slow"))  # fmt: skip
    # Alice is quinn-gh here, who may push to web-app.
    ref = await ask(world, "implement the plan", thread_ts=first.message_ts, user="U0ALICE")
    u = await understood(world, ref)
    case = await world.runner.prepare_change(ref, u)
    assert case.stopped is None, case.stopped
    assert case.recipe is None
    assert "No test setup was detected for this repository" in case.notes
    implemented = await world.runner.implement(ref, u, case, "standard", {})
    verification = await world.runner.verify_change(ref, u, case)
    assert (verification.ok, verification.ran) == (False, False)
    exported = await world.runner.export_change(ref, u, case, verification, None, implemented)
    assert [f["path"] for f in exported.files] == ["search/index.ts"]
    assert "No test setup was detected, so the tests could not be run." in exported.ask
    assert "The change adds or edits no test." in exported.ask
    assert exported.action is not None
    assert exported.action.ask
    composed = await world.runner.report_change(
        ref, u, case, [implemented], verification, None, exported, None
    )
    assert "The tests could not be run" in composed.not_verified


async def test_without_a_plan_or_push_access_nothing_is_prepared(world: Agent) -> None:
    ref = await ask(world, "implement the plan")
    u = Understanding("implement_change", 1, 0.9, None, "ask", brief={"goal": "implement", "entities": {}})
    case = await world.runner.prepare_change(ref, u)
    assert case.stopped is not None
    assert case.stopped.startswith("There's no plan in this thread to implement yet.")
    # Alice (quinn-gh) can read cart-service but not push to it.
    first = await thread_with_plan(world, plan_for("src/pricing/cart.py"))
    hers = await ask(world, "go ahead and implement the plan", thread_ts=first.message_ts, user="U0ALICE")
    case = await world.runner.prepare_change(hers, await understood(world, hers))
    assert case.stopped == "your GitHub account (quinn-gh) can't push to acme/cart-service."
    assert world.sql("select count(*) from app.sandboxes where task_id = %s", (hers.task_id,)) == [(0,)]


# ------------------------------------------------------------------ the workflow


async def test_verification_failing_twice_stops_without_a_pull_request(world: Agent, live: Live) -> None:
    channel = await own_channel(world)  # everyone here can read the repository: the report is posted here
    plan = plan_for("src/webhooks/verify.py", goal="Webhooks fail after a rotation")
    first = await thread_with_plan(world, plan, channel=channel)
    event_id, _ = await live.request(
        "go ahead and implement the plan", channel=channel, thread_ts=first.message_ts
    )
    assert await asyncio_result(live, event_id) == "stopped"
    handle = live.client.get_workflow_handle(f"task-{event_id}")
    activities = []
    async for event in handle.fetch_history_events():
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            activities.append(event.activity_task_scheduled_event_attributes.activity_type.name)
    assert activities.count("agent_implement") == 2
    assert activities.count("agent_verify_change") == 2
    assert "agent_export_change" not in activities
    task_id = live.task_row(event_id)[0]
    assert world.sql("select count(*) from app.task_links where task_id = %s", (task_id,)) == [(0,)]
    assert world.sql("select status from app.sandboxes where task_id = %s", (task_id,)) == [("destroyed",)]

    async def stopped() -> Any:
        return [m for m in thread_of(await channel_messages(channel), first.message_ts)
                if "the tests still fail after 2 attempts" in json.dumps(m)]  # fmt: skip

    assert await wait_for(stopped, 60)


async def test_a_rejected_push_pushes_nothing(world: Agent, live: Live) -> None:
    channel = await own_channel(world)
    first = await thread_with_plan(world, plan_for("src/pricing/cart.py"), channel=channel)
    event_id, _ = await live.request(
        "go ahead and implement the plan", channel=channel, thread_ts=first.message_ts
    )
    approval_id, message_ts = await live.approval(event_id)
    assert (
        await live.click("U0BOB", "approval_reject", approval_id, channel=channel, ts=message_ts)
    ).status_code == 200
    assert await asyncio_result(live, event_id) == "stopped"
    task_id = live.task_row(event_id)[0]
    assert world.sql("select count(*) from app.task_links where task_id = %s", (task_id,)) == [(0,)]
    assert (
        world.sql(
            "select status from app.external_actions where task_id = %s and integration_key = 'github'",
            (task_id,),
        )
        == []
    )
    branch = world.sql("select content->>'branch' from app.artifacts where task_id = %s and kind = 'diff'",
                       (task_id,))[0][0]  # fmt: skip
    assert branch not in github_state()["refs"]

    async def reported() -> Any:
        return [m for m in thread_of(await channel_messages(channel), first.message_ts)
                if "Not pushed*: the push was rejected." in json.dumps(m)]  # fmt: skip

    assert await wait_for(reported, 60)


async def test_review_comments_come_back_and_revise_the_same_pull_request(world: Agent, live: Live,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    from app.settings import get_settings

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("TEMPORAL_TASK_QUEUE", live.settings.temporal_task_queue)  # not the dev stack's worker
    get_settings.cache_clear()
    set_auto(world, True)
    try:
        channel = await own_channel(world)
        first = await thread_with_plan(world, plan_for("src/pricing/cart.py"), channel=channel)
        event_id, _ = await live.request("go ahead and implement the plan", channel=channel,
                                         thread_ts=first.message_ts)  # fmt: skip
        assert await asyncio_result(live, event_id) == "completed"
        task_id = live.task_row(event_id)[0]
        (link,) = world.sql("select external_id, url from app.task_links where task_id = %s", (task_id,))
        number = int(link[0].rsplit("#", 1)[1])
        messages = thread_of(await channel_messages(channel), first.message_ts)
        assert any(f"Opened <{link[1]}|{REPO}#{number}>" in json.dumps(m) for m in messages)
        # A reviewer asks for a change on GitHub; GitHub's webhook reaches our API.
        async with httpx.AsyncClient() as client:
            await client.post(f"{FAKES}/_admin/github/review", json={
                "repository": REPO, "number": number, "login": "bob-gh", "state": "changes_requested",
                "body": "Nearly there", "comments": [{"path": "src/pricing/cart.py", "line": 1,
                                                      "body": "Please add a docstring to total"}]})  # fmt: skip
            deliveries = (await client.get(f"{FAKES}/_admin/github/deliveries")).json()
        delivery = next(d for d in reversed(deliveries) if d["event"] == "pull_request_review"
                        and d["payload"]["pull_request"]["number"] == number)  # fmt: skip
        body = json.dumps(delivery["payload"]).encode()
        accepted = await send_webhook(live, "pull_request_review", delivery["id"], body)
        assert accepted.json() == {"status": "accepted", "notices": 1}
        assert (await send_webhook(live, "pull_request_review", delivery["id"], body)).json() == {
            "status": "duplicate"
        }

        async def notice() -> Any:
            return [m for m in thread_of(await channel_messages(channel), first.message_ts)
                    if "requested changes on" in json.dumps(m)]  # fmt: skip

        (posted,) = await wait_for(notice, 60)
        assert "Please add a docstring to total" in json.dumps(
            posted
        )  # everyone here can read the repository
        assert "address the review comments" in json.dumps(posted)
        # "Address the review comments" in the thread revises the same pull request.
        before = github_state()["refs"]
        branch = next(b for b, sha in before.items() if b.startswith("agent/") and
                      any(p["number"] == number and p["head"]["ref"] == b for p in github_state()["pulls"]))  # fmt: skip
        async with httpx.AsyncClient() as client:  # CI fails on the revision's push
            await client.post(f"{FAKES}/_admin/github/ci", json={"repository": REPO, "conclusion": "failure",
                                                                 "failure_summary": "1 failed: test_rounding"})  # fmt: skip
        event2, _ = await live.request("please address the review comments", channel=channel,
                                       thread_ts=first.message_ts)  # fmt: skip
        # The revision adds no test, so it asks a person even in Auto.
        approval_id, message_ts = await live.approval(event2)
        summary = world.sql("select action_summary from app.approvals where id = %s", (approval_id,))[0][0]
        assert "Needs a person: The change adds or edits no test." in summary
        assert (
            await live.click("U0BOB", "approval_approve", approval_id, channel=channel, ts=message_ts)
        ).status_code == 200
        assert await asyncio_result(live, event2) == "completed"
        after = github_state()
        assert after["refs"][branch] != before[branch]
        last = [p for p in after["pushes"] if p["ref"] == f"refs/heads/{branch}"][-1]
        assert (last["before"], last["forced"]) == (before[branch], False)
        commit = next(c for c in after["commits"] if c["sha"] == after["refs"][branch])
        assert [f["filename"] for f in commit["files"]] == ["src/pricing/cart.py"]
        assert "Return the sum of the items' prices." in commit["files"][0]["patch"]
        assert len([p for p in after["pulls"] if p["head"]["ref"] == branch]) == 1
        assert any(c["number"] == number and c["body"].startswith("Pushed ") for c in after["issue_comments"])

        async def revised() -> Any:
            return [m for m in thread_of(await channel_messages(channel), first.message_ts)
                    if f"Pushed a revision to <{link[1]}|{REPO}#{number}>" in json.dumps(m)]  # fmt: skip

        assert await wait_for(revised, 60)

        async def failed_suite() -> Any:
            async with httpx.AsyncClient() as client:
                found = (await client.get(f"{FAKES}/_admin/github/deliveries")).json()
            return next((d for d in reversed(found) if d["event"] == "check_suite"
                         and d["payload"]["check_suite"]["head_branch"] == branch
                         and d["payload"]["check_suite"]["conclusion"] == "failure"), None)  # fmt: skip

        suite = await wait_for(failed_suite, 30)
        assert [p["number"] for p in suite["payload"]["check_suite"]["pull_requests"]] == [number]
        posted_suite = await send_webhook(
            live, "check_suite", suite["id"], json.dumps(suite["payload"]).encode()
        )
        assert posted_suite.json()["status"] == "accepted"

        async def checks_failed() -> Any:
            return [m for m in thread_of(await channel_messages(channel), first.message_ts)
                    if f"Checks failed on <{link[1]}|{REPO}#{number}>" in json.dumps(m)]  # fmt: skip

        (ci,) = await wait_for(checks_failed, 60)
        assert "1 failed: test_rounding" in json.dumps(ci)
        assert "fix CI" in json.dumps(ci)
    finally:
        set_auto(world, False)
        get_settings.cache_clear()


async def asyncio_result(live: Live, event_id: str) -> Any:
    import asyncio

    return await asyncio.wait_for(live.client.get_workflow_handle(f"task-{event_id}").result(), 180)


async def send_webhook(
    live: Live, event: str, delivery: str, body: bytes, secret: str = WEBHOOK_SECRET
) -> httpx.Response:
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return await live.api.post("/webhooks/github", content=body, headers={
        "x-github-event": event, "x-github-delivery": delivery, "x-hub-signature-256": signature,
        "content-type": "application/json"})  # fmt: skip


async def test_webhooks_are_verified_and_unknown_pull_requests_ignored(world: Agent, live: Live,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    from app.settings import get_settings

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    get_settings.cache_clear()
    try:
        payload = {"action": "submitted", "repository": {"full_name": REPO}, "pull_request": {"number": 999999},
                   "review": {"id": 1, "state": "COMMENTED", "user": {"login": "bob-gh"}}}  # fmt: skip
        body = json.dumps(payload).encode()
        forged = await send_webhook(live, "pull_request_review", "d-forged", body, secret="wrong")
        assert forged.status_code == 401
        unsigned = await live.api.post("/webhooks/github", content=body, headers={"x-github-event": "ping"})
        assert unsigned.status_code == 401
        unknown = await send_webhook(live, "pull_request_review", f"d-{uuid.uuid4().hex}", body)
        assert (unknown.status_code, unknown.json()["status"]) == (202, "ignored")
        other = await send_webhook(
            live, "push", f"d-{uuid.uuid4().hex}", b'{"repository": {"full_name": "a/b"}}'
        )
        assert other.status_code == 202
        assert (await send_webhook(live, "ping", "d-ping", b"{}")).json() == {"status": "pong"}
    finally:
        get_settings.cache_clear()
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET")
    get_settings.cache_clear()
    assert (await send_webhook(live, "ping", "d-x", b"{}")).status_code == 503  # not configured
    get_settings.cache_clear()

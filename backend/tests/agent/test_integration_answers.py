"""The agent answers Jira and GitHub questions with the right credential per operation, refuses what
the requester cannot see, and keeps restricted answers private when the channel could not see them."""

from collections.abc import Iterator
from typing import Any

import pytest
import pytest_asyncio

from app.agent.types import TaskRef
from app.slack.adapter import DirectMessage, Reply
from tests.agent.conftest import Agent, ui_post
from tests.connect import connect_github, connect_jira, enable, link_github, reset_jira_issues, sql


@pytest.fixture(scope="module", autouse=True)
def _jira_issues_as_seeded() -> None:
    reset_jira_issues()


@pytest_asyncio.fixture(loop_scope="session", autouse=True)
async def connected(agent: Agent) -> None:
    stack, acme = agent.stack, agent.world.acme
    for key in ("jira", "github"):
        enable(stack, acme, key)
    await connect_jira(stack, acme, "acc-alice")
    await connect_jira(stack, acme, "acc-bob", acme.user)
    await connect_github(stack, acme)
    await link_github(stack, acme, acme.user, "bob-gh")


@pytest.fixture(autouse=True)
def _close(agent: Agent) -> Iterator[None]:
    yield
    agent.sql("update app.tasks set status = 'completed', finished_at = now() where org_id = %s "
              "and status in ('queued', 'running', 'awaiting_approval')", (agent.world.acme.id,))  # fmt: skip
    agent.sql("delete from app.operation_policies where org_id = %s", (agent.world.acme.id,))


async def ask(agent: Agent, text: str, user: str = "U0BOB", channel: str = "C0BUGS") -> TaskRef:
    message = await ui_post(user, channel, text)
    ref = await agent.task(user, channel, ts=message["ts"])
    assert (await agent.runner.start(ref)).status == "started"
    return ref


async def answer(agent: Agent, ref: TaskRef) -> tuple[Any, Any, Any, Any]:
    u = await agent.runner.understand(ref, "ask")
    f = await agent.runner.gather(ref, u, "standard")
    c = await agent.runner.compose(ref, u, f, "standard")
    mark = len(agent.sink.sent)
    delivered = await agent.runner.deliver(ref, u, c, [])
    return u, f, c, (delivered, [i for i, _ in agent.sink.sent[mark:]])


async def test_a_jira_question_is_answered_with_its_source(agent: Agent) -> None:
    ref = await ask(agent, "what is the status of PAY-101?")
    u, f, c, (delivered, sent) = await answer(agent, ref)
    assert u.playbook_version == 2
    assert u.brief["entities"]["ids"] == ["PAY-101"]
    assert any("PAY-101: Checkout returns 500" in p["text"] for p in f.points)
    assert c.sources[0]["permalink"] == "https://acme.atlassian.net/browse/PAY-101"
    # Everyone in #bugs has a Jira account that can browse PAY: the answer stays in the thread.
    assert delivered.where == "thread"
    assert isinstance(sent[0], Reply)
    assert "PAY-101" in sent[0].text
    audited = agent.sql(
        "select target, decision, metadata->>'credential_mode' from app.audit_log "
        "where task_id = %s and action = 'policy.tool'",
        (ref.task_id,),
    )
    assert ("jira.get_issue", "allowed", "org_with_check") in audited


async def test_a_restricted_project_is_refused(agent: Agent) -> None:
    ref = await ask(agent, "what is happening with SEC-3?")
    _, f, c, _ = await answer(agent, ref)
    assert f.points == [] or all("SEC-3" not in p["text"] for p in f.points)
    assert any("can't browse the project of SEC-3" in g for g in f.gaps)
    assert "staging token" not in c.answer


async def test_a_restricted_answer_is_sent_privately_in_a_mixed_channel(agent: Agent) -> None:
    ref = await ask(agent, "what is the status of PAY-101?", channel="C0GENERAL")
    _, _, _, (delivered, sent) = await answer(agent, ref)
    assert delivered.where == "private"  # #general has people who are not org members
    assert isinstance(sent[0], DirectMessage)
    assert "PAY-101" in sent[0].text
    assert "PAY-101" not in sent[1].text


async def test_a_github_question_about_owners_and_code(agent: Agent) -> None:
    ref = await ask(agent, "who owns src/pricing/cart.py in acme/cart-service?")
    u, f, _, (delivered, _) = await answer(agent, ref)
    assert u.brief["entities"]["repositories"] == ["acme/cart-service"]
    texts = " ".join(p["text"] for p in f.points)
    assert "@bob-gh" in texts  # from CODEOWNERS
    assert "assumes at most 19 items" in texts  # from the file
    # A private repository: not everyone in #bugs has a linked GitHub account that can read it.
    assert delivered.where == "private"
    infra = await ask(agent, "who owns acme/infra?")
    _, f, _, _ = await answer(agent, infra)
    assert any("can't read acme/infra" in g for g in f.gaps)


async def test_an_override_to_user_required_uses_each_persons_own_account(agent: Agent) -> None:
    acme = agent.world.acme
    sql(agent.stack, "insert into app.operation_policies (org_id, integration_key, operation, credential_mode) "
                     "values (%s, 'jira', 'get_issue', 'user_required')", (acme.id,))  # fmt: skip
    alice = await ask(agent, "what is the status of PAY-102?", user="U0ALICE")
    _, f, _, _ = await answer(agent, alice)
    assert any("Link your Jira account" in g for g in f.gaps)  # Alice has no link of her own
    bob = await ask(agent, "what is the status of PAY-102?")
    _, f, _, _ = await answer(agent, bob)
    assert any("PAY-102" in p["text"] for p in f.points)
    audited = agent.sql(
        "select metadata->>'credential_mode' from app.audit_log where task_id = %s and target = "
        "'jira.get_issue'",
        (bob.task_id,),
    )
    assert audited == [("user_required",)]


@pytest.mark.parametrize("disabled", ["jira"])
async def test_disabled_integrations_are_not_offered_to_the_model(agent: Agent, disabled: str) -> None:
    from tests.agent.conftest import model_requests, reset_model

    enable(agent.stack, agent.world.acme, disabled, enabled=False)
    try:
        await reset_model()
        ref = await ask(agent, "what is the status of PAY-101?")
        u = await agent.runner.understand(ref, "ask")
        await agent.runner.gather(ref, u, "standard")
        offered = {t["name"] for r in await model_requests() for t in r.get("tools") or []}
        assert not any(name.startswith("jira_") for name in offered)
        assert "github_read_file" in offered
    finally:
        enable(agent.stack, agent.world.acme, disabled)

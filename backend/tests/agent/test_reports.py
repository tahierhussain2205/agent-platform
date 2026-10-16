"""Scheduled report runs: figures equal what direct queries of Jira and Slack give, the narrative
keeps only known numbers, every run is checked against the owner's current access, and access
or budget problems pause the schedule and notify the admins once."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from psycopg.types.json import Jsonb

from app.agent.reports import ReportRunner
from app.agent.runner import AgentDeps
from app.agent.types import ReportStart
from app.schedules.figures import parse_jira_time
from app.slack.adapter import DirectMessage, Report
from tests.agent.conftest import Agent, script_model, ui_post
from tests.connect import connect_jira, enable, reset_jira_issues
from tests.stack import FAKES


@pytest.fixture(scope="module", autouse=True)
def _jira_issues_as_seeded() -> None:
    reset_jira_issues()


JIRA = f"{FAKES}/atlassian/api/ex/jira/cloud-acme/rest/api/3"


@pytest_asyncio.fixture(loop_scope="session")
async def reports(agent: Agent) -> AsyncIterator[ReportRunner]:
    enable(agent.stack, agent.world.acme, "jira")
    await connect_jira(agent.stack, agent.world.acme, "acc-alice")
    yield ReportRunner(agent.runner.d)
    agent.sql(
        "update app.schedules set status = 'deleted', pause_reason = null where org_id = %s",
        (agent.world.acme.id,),
    )


def membership(agent: Agent, email: str) -> Any:
    return agent.sql(
        "select m.id from app.org_memberships m join app.users u on u.id = m.user_id "
        "where m.org_id = %s and u.email = %s",
        (agent.world.acme.id, email),
    )[0][0]


def channel(agent: Agent, name: str) -> Any:
    return agent.sql(
        "select id from app.channels where org_id = %s and name = %s", (agent.world.acme.id, name)
    )[0][0]


def schedule(
    agent: Agent, kind: str, params: dict[str, Any], *, owner: str = "bob@acme.test", deliver_to: str | None = None,
    cron: str | None = None,
) -> str:  # fmt: skip
    now = next_minute()
    rows = agent.sql(
        "insert into app.schedules (org_id, owner_membership_id, name, instruction, playbook_key, params, cron, "
        "timezone, delivery_type, delivery_channel_id) values (%s, %s, %s, 'report', %s, %s, %s, 'UTC', %s, %s) "
        "returning id",
        (
            agent.world.acme.id, membership(agent, owner), f"Report {uuid.uuid4().hex[:6]}", kind, Jsonb(params),
            cron or f"{now.minute} {now.hour} * * *",  # daily at the next minute: the window is a day
            "channel" if deliver_to else "dm", channel(agent, deliver_to) if deliver_to else None,
        ),
    )  # fmt: skip
    return str(rows[0][0])


def next_minute() -> datetime:
    return (datetime.now(UTC) + timedelta(minutes=1)).replace(second=0, microsecond=0)


def scheduled_now() -> str:
    """The next run's time: messages posted just now are inside its window (it ends at the run)."""
    return next_minute().isoformat()


async def run(
    reports: ReportRunner, agent: Agent, sid: str, at: str | None = None
) -> tuple[ReportStart, Any]:
    start = await reports.begin(
        str(agent.world.acme.id), sid, at or scheduled_now(), f"report-{sid}-{uuid.uuid4()}"
    )
    assert start.status == "run", start.message
    assert start.ref is not None
    assert (await reports.start(start.ref)).status == "started"
    return start, await reports.compose_report(start.ref, start)


def sent_reports(agent: Agent, mark: int) -> list[Report]:
    return [i for i, _ in agent.sink.sent[mark:] if isinstance(i, Report)]


async def jira_issues(account: str = "acc-bob") -> list[dict[str, Any]]:
    """Every PAY issue straight from the Jira stand-in, as the account sees it."""
    from tests.connect import consent

    code = (
        await consent("/atlassian/authorize", account, redirect_uri="http://x/cb", state="s", scope="read")
    )["code"]
    async with httpx.AsyncClient() as client:
        token = (
            await client.post(
                f"{FAKES}/atlassian/oauth/token",
                json={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": "http://x/cb",
                    "client_id": "local-jira-client",
                    "client_secret": "local-jira-client-secret",
                },
            )
        ).json()["access_token"]
        body = {"jql": "project = PAY", "maxResults": 100, "fields": ["status", "created", "resolutiondate"]}
        found = await client.post(
            f"{JIRA}/search/jql", json=body, headers={"authorization": f"Bearer {token}"}
        )
    return list(found.json()["issues"])


# ------------------------------------------------------------------ figures against direct queries


async def test_jira_figures_equal_direct_queries(agent: Agent, reports: ReportRunner) -> None:
    sid = schedule(agent, "report_jira_progress", {"project": "PAY"}, deliver_to="general")
    start, composed = await run(reports, agent, sid)
    begin, end = datetime.fromisoformat(start.window_start), datetime.fromisoformat(start.window_end)
    assert end - begin == timedelta(days=1)
    issues = await jira_issues()

    def within(value: str | None) -> bool:
        when = parse_jira_time(value)
        return when is not None and begin <= when < end

    expected_created = sorted(i["key"] for i in issues if within(i["fields"]["created"]))
    expected_resolved = sorted(i["key"] for i in issues if within(i["fields"]["resolutiondate"]))
    expected_open = [i for i in issues if i["fields"]["status"]["statusCategory"]["key"] != "done"]
    f = composed.figures
    assert f["created_keys"] == expected_created
    assert f["resolved_keys"] == expected_resolved
    assert (f["created"], f["resolved"], f["open"]) == (
        len(expected_created),
        len(expected_resolved),
        len(expected_open),
    )
    assert sum(n for _, n in f["open_by_status"]) == len(expected_open)
    assert composed.figure_lines[0] == (
        f"Created: *{f['created']}* · Resolved: *{f['resolved']}* · Open now: *{f['open']}*"
    )
    assert composed.highlights
    assert all(h["refs"] for h in composed.highlights)
    assert all(s["kind"] == "jira" and s["permalink"].startswith("https://acme.atlassian.net/browse/")
               for s in composed.sources)  # fmt: skip
    # Delivered to #general: a public channel cannot see Jira's restricted sources unless every member
    # is identified and may browse PAY; the seeded #general has people who are not, so it goes privately.
    mark = len(agent.sink.sent)
    delivered = await reports.deliver_report(start.ref, start, composed)  # type: ignore[arg-type]
    (report,) = sent_reports(agent, mark)
    assert delivered.where == "private"
    assert "I couldn't post this report in #general" in report.sections[0][1]
    assert any(composed.figure_lines[0] in body for _, body in report.sections)
    last_run = agent.sql("select last_run_at from app.schedules where id = %s", (sid,))[0][0]
    assert last_run == datetime.fromisoformat(start.scheduled_at)


async def test_channel_figures_equal_conversations_history(agent: Agent, reports: ReportRunner) -> None:
    t = uuid.uuid4().hex[:6]
    first = await ui_post("U0QUINN", "C0BUGS", f"Checkout fails again {t}")
    await ui_post("U0BOB", "C0BUGS", f"Looking into it {t}", thread_ts=first["ts"])
    await ui_post("U0ALICE", "C0BUGS", f"Rolled back to v41 {t}", thread_ts=first["ts"])
    await ui_post("U0ALICE", "C0BUGS", f"Release notes are out {t}")
    sid = schedule(agent, "report_channel_summary", {"channels": [str(channel(agent, "bugs"))]})
    start, composed = await run(reports, agent, sid)
    oldest = datetime.fromisoformat(start.window_start).timestamp()
    latest = datetime.fromisoformat(start.window_end).timestamp()
    async with httpx.AsyncClient() as client:  # what conversations.history and .replies say directly
        token = {"authorization": "Bearer xoxb-local-T0ACME"}
        history = (
            await client.post(f"{FAKES}/slack/api/conversations.history", headers=token,
                              data={"channel": "C0BUGS", "oldest": oldest, "latest": latest, "limit": 1000})
        ).json()["messages"]  # fmt: skip
        top = [m for m in history if m.get("user") != "U0ACMEBOT" and m.get("subtype") is None]
        replies = 0
        people = {m["user"] for m in top if m.get("user")}
        for parent in [m for m in top if m.get("reply_count")]:
            thread = (
                await client.post(f"{FAKES}/slack/api/conversations.replies", headers=token,
                                  data={"channel": "C0BUGS", "ts": parent["ts"]})
            ).json()["messages"]  # fmt: skip
            mine = [r for r in thread if r["ts"] != parent["ts"] and r.get("user") != "U0ACMEBOT"]
            replies += len(mine)
            people |= {r["user"] for r in mine if r.get("user")}
    assert composed.figures["total"] == {
        "messages": len(top),
        "threads": len([m for m in top if m.get("reply_count")]),
        "replies": replies,
        "people": len(people),
    }
    assert composed.figures["total"]["messages"] >= 2
    assert composed.figures["total"]["replies"] >= 2
    assert any(f"Release notes are out {t}" in h["text"] for h in composed.highlights)
    assert all(s["channel_external_id"] == "C0BUGS" for s in composed.sources)
    mark = len(agent.sink.sent)
    delivered = await reports.deliver_report(start.ref, start, composed)  # type: ignore[arg-type]
    assert delivered.where == "dm"
    (report,) = sent_reports(agent, mark)
    assert report.channel.startswith("D")
    assert report.sections[-1][0] == "Sources"


async def test_invented_numbers_are_dropped_from_the_narrative(agent: Agent, reports: ReportRunner) -> None:
    sid = schedule(agent, "report_jira_progress", {"project": "PAY"})
    start = await reports.begin(str(agent.world.acme.id), sid, scheduled_now(), f"report-{sid}-x")
    assert start.ref is not None
    await reports.start(start.ref)
    await script_model(
        [
            {
                "type": "tool_use",
                "name": "submit_report_narrative",
                "input": {
                    "summary": "Velocity rose 40% this week.",
                    "highlights": [
                        {"text": "About 17 issues are stuck in review.", "refs": ["S1"]},
                        {"text": "PAY-101 is still in progress.", "refs": ["S1"]},
                    ],
                },
            }
        ]
    )
    composed = await reports.compose_report(start.ref, start)
    assert composed.summary == ""
    assert [h["text"] for h in composed.highlights] == ["PAY-101 is still in progress."]
    assert composed.dropped == 2
    artifact = agent.sql(
        "select content from app.artifacts where task_id = %s and kind = 'report'", (start.ref.task_id,)
    )[0][0]
    assert artifact["dropped"] == 2
    # The invented numbers are gone from what was kept (not "17" anywhere: the window has times of day).
    kept = str([artifact["summary"], artifact["highlights"]])
    assert "17" not in kept
    assert "40%" not in kept


# ------------------------------------------------------------------ delivery


async def test_a_private_channel_summary_goes_to_the_owner_not_a_public_channel(
    agent: Agent, reports: ReportRunner
) -> None:
    await ui_post("U0ALICE", "C0LEADS", f"Offsite moved {uuid.uuid4().hex[:6]}")
    sid = schedule(
        agent, "report_channel_summary", {"channels": [str(channel(agent, "leads-private"))]},
        owner="alice@acme.test", deliver_to="general",
    )  # fmt: skip
    start, composed = await run(reports, agent, sid)
    mark = len(agent.sink.sent)
    delivered = await reports.deliver_report(start.ref, start, composed)  # type: ignore[arg-type]
    (report,) = sent_reports(agent, mark)
    assert delivered.where == "private"
    assert report.channel.startswith("D")  # the owner's DM, not #general


async def test_delivery_is_idempotent_and_runs_are_keyed(agent: Agent, reports: ReportRunner) -> None:
    sid = schedule(agent, "report_jira_progress", {"project": "PAY"})
    workflow_id = f"report-{sid}-{uuid.uuid4()}"
    first = await reports.begin(str(agent.world.acme.id), sid, scheduled_now(), workflow_id)
    again = await reports.begin(str(agent.world.acme.id), sid, scheduled_now(), workflow_id)  # a retry
    assert first.ref is not None
    assert again.ref is not None
    assert first.ref.task_id == again.ref.task_id
    await reports.start(first.ref)
    composed = await reports.compose_report(first.ref, first)
    mark = len(agent.sink.sent)
    await reports.deliver_report(first.ref, first, composed)
    await reports.deliver_report(first.ref, first, composed)
    assert len(sent_reports(agent, mark)) == 1


# ------------------------------------------------------------------ access is checked at every run


async def test_paused_and_deleted_schedules_skip(agent: Agent, reports: ReportRunner) -> None:
    sid = schedule(agent, "report_jira_progress", {"project": "PAY"})
    agent.sql("update app.schedules set status = 'paused', pause_reason = 'admin' where id = %s", (sid,))
    start = await reports.begin(str(agent.world.acme.id), sid, scheduled_now(), f"report-{sid}-p")
    assert (start.status, start.paused) == ("skip", False)
    agent.sql("update app.schedules set status = 'deleted', pause_reason = null where id = %s", (sid,))
    assert (
        await reports.begin(str(agent.world.acme.id), sid, scheduled_now(), f"report-{sid}-d")
    ).status == "skip"
    assert agent.sql("select count(*) from app.tasks where schedule_id = %s", (sid,)) == [(0,)]


async def test_losing_a_source_pauses_with_the_reason(agent: Agent, reports: ReportRunner) -> None:
    # Bob is not a member of #leads-private: the policy refuses the read at run time.
    sid = schedule(agent, "report_channel_summary", {"channels": [str(channel(agent, "leads-private"))]})
    _start, composed = await run(reports, agent, sid)
    assert composed.denied
    assert "leads-private" in composed.denied
    assert await reports.pause(str(agent.world.acme.id), sid, "access_lost", composed.denied)
    row = agent.sql("select status, pause_reason, pause_detail from app.schedules where id = %s", (sid,))[0]
    assert row[:2] == ("paused", "access_lost")
    assert "leads-private" in row[2]


async def test_a_jira_project_the_owner_cannot_browse_is_access_lost(
    agent: Agent, reports: ReportRunner
) -> None:
    sid = schedule(agent, "report_jira_progress", {"project": "SEC"})  # only Alice may browse SEC
    _, composed = await run(reports, agent, sid)
    assert composed.denied == "Access denied: you can't browse the SEC project."


async def test_a_delivery_channel_off_the_allowlist_pauses_at_begin(
    agent: Agent, reports: ReportRunner
) -> None:
    sid = schedule(agent, "report_jira_progress", {"project": "PAY"}, deliver_to="random")  # bot not in it
    start = await reports.begin(str(agent.world.acme.id), sid, scheduled_now(), f"report-{sid}-r")
    assert start.status == "skip"
    assert start.paused
    row = agent.sql("select status, pause_reason, pause_detail from app.schedules where id = %s", (sid,))[0]
    assert row[0] == "paused"
    assert row[1] == "access_lost"


async def test_a_spent_budget_pauses(agent: Agent, reports: ReportRunner) -> None:
    sid = schedule(agent, "report_jira_progress", {"project": "PAY"})
    agent.sql(
        "update app.org_settings set org_monthly_token_limit = 1 where org_id = %s", (agent.world.acme.id,)
    )
    agent.sql(
        "insert into app.usage_events (org_id, kind, model, input_tokens, output_tokens) "
        "values (%s, 'llm', 'claude-sonnet-5', 10, 10)",
        (agent.world.acme.id,),
    )
    try:
        start = await reports.begin(str(agent.world.acme.id), sid, scheduled_now(), f"report-{sid}-b")
    finally:
        agent.sql("update app.org_settings set org_monthly_token_limit = null where org_id = %s",
                  (agent.world.acme.id,))  # fmt: skip
    assert start.status == "skip"
    assert start.paused
    assert agent.sql("select pause_reason from app.schedules where id = %s", (sid,)) == [("budget",)]


async def test_removing_the_owner_pauses_and_notifies_admins_once(
    agent: Agent, reports: ReportRunner
) -> None:
    acme = agent.world.acme
    quinn = membership(agent, "quinn@acme.test")
    sid = schedule(agent, "report_jira_progress", {"project": "PAY"}, owner="quinn@acme.test")
    name = agent.sql("select name from app.schedules where id = %s", (sid,))[0][0]
    agent.sql("update app.org_memberships set status = 'removed' where id = %s", (quinn,))
    try:
        assert agent.sql("select status, pause_reason from app.schedules where id = %s", (sid,)) == [
            ("paused", "owner_removed")
        ]
        start = await reports.begin(str(acme.id), sid, scheduled_now(), f"report-{sid}-o")
        assert start.status == "skip"
        mark = len(agent.sink.sent)
        assert await reports.notify_pauses(str(acme.id)) >= 1
        notices = [i for i, _ in agent.sink.sent[mark:] if isinstance(i, DirectMessage) and name in i.text]
        to_admins = [n for n in notices if "owned by quinn@acme.test" in n.text]
        assert to_admins == notices  # Quinn is removed: no notice to the owner
        assert {n.user for n in to_admins} == {"U0ALICE"}  # the active admin
        assert "its owner was removed from the organization" in to_admins[0].text
        assert "Reassign it or delete it" in to_admins[0].text
        assert "http://console.test/schedules" in to_admins[0].text
        mark = len(agent.sink.sent)
        assert await reports.notify_pauses(str(acme.id)) == 0  # told once
        assert agent.sink.sent[mark:] == []
    finally:
        agent.sql("update app.org_memberships set status = 'active' where id = %s", (quinn,))


async def test_concurrent_syncs_send_a_pause_notice_once_and_a_failed_send_is_retried(
    agent: Agent, reports: ReportRunner
) -> None:
    acme = agent.world.acme
    quinn = membership(agent, "quinn@acme.test")
    sid = schedule(agent, "report_jira_progress", {"project": "PAY"}, owner="quinn@acme.test")
    name = agent.sql("select name from app.schedules where id = %s", (sid,))[0][0]
    agent.sql("update app.org_memberships set status = 'removed' where id = %s", (quinn,))

    class Down:
        async def send(self, intent: Any, **_: Any) -> Any:
            raise RuntimeError("Slack is down")

    class Slow:  # takes a moment, like Slack, so both syncs reach the check before either sends
        def __init__(self) -> None:
            self.sent: list[Any] = []

        async def send(self, intent: Any, **_: Any) -> Any:
            await asyncio.sleep(0.2)
            self.sent.append(intent)
            return "1.0"

    def runner(sink: Any) -> ReportRunner:
        return ReportRunner(
            AgentDeps(**{**agent.runner.d.__dict__, "sink_factory": lambda team, token: sink})
        )

    try:
        with pytest.raises(RuntimeError):
            await runner(Down()).notify_pauses(str(acme.id))  # the claim is released, nothing is marked
        slow = Slow()
        await asyncio.gather(
            runner(slow).notify_pauses(str(acme.id)), runner(slow).notify_pauses(str(acme.id))
        )
        notices = [i for i in slow.sent if isinstance(i, DirectMessage) and name in i.text]
        assert [n.user for n in notices] == ["U0ALICE"]  # one notice, though two syncs ran at once
    finally:
        agent.sql("update app.org_memberships set status = 'active' where id = %s", (quinn,))

"""Scheduled reports through Temporal: schedules mirrored as Temporal Schedules, runs delivered to
the Slack stand-in, a schedule that fires on time, and paused or deleted schedules that stop."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from psycopg.types.json import Jsonb
from temporalio.client import Client
from temporalio.service import RPCError

from app.agent.reports import ReportRunner
from app.agent.runner import AgentDeps, AgentRunner
from app.schedules.cron import Cadence
from app.schedules.temporal import sync_org, temporal_id
from app.settings import Settings, get_settings
from app.temporal import connect_temporal
from app.worker import build_worker
from app.workflows.report_activities import ReportActivities
from app.workflows.slack_activities import SlackActivities, SlackDeps
from app.workflows.slack_types import HomeRequest
from app.workflows.task_activities import TaskActivities
from tests.agent.conftest import Agent, channel_messages
from tests.agent.live import SIGNING_SECRET, TEMPORAL, temporal_up, wait_for
from tests.agent.live import Live as TaskLive
from tests.connect import connect_jira, enable, reset_jira_issues
from tests.stack import FAKES, PROVIDER_SETTINGS, Stack, app_for

pytestmark = pytest.mark.skipif(not temporal_up(), reason="Temporal not reachable (run `make infra`)")


@pytest.fixture(scope="module", autouse=True)
def _jira_issues_as_seeded() -> None:
    reset_jira_issues()


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SIGNING_SECRET)


@dataclass
class Live:
    agent: Agent
    client: Client
    settings: Settings
    slack: TaskLive  # intake, clicks and requests through the API (test_task_workflow's helpers)

    @property
    def org(self) -> str:
        return str(self.agent.world.acme.id)

    def schedule(self, cron: str, *, deliver_to: str | None = None, owner: str = "bob@acme.test") -> str:
        a = self.agent
        member = a.sql(
            "select m.id from app.org_memberships m join app.users u on u.id = m.user_id "
            "where m.org_id = %s and u.email = %s",
            (a.world.acme.id, owner),
        )[0][0]
        channel = (
            a.sql(
                "select id from app.channels where org_id = %s and name = %s", (a.world.acme.id, deliver_to)
            )[0][0]
            if deliver_to
            else None
        )
        return str(
            a.sql(
                "insert into app.schedules (org_id, owner_membership_id, name, instruction, playbook_key, params, "
                "cron, timezone, delivery_type, delivery_channel_id) values (%s, %s, %s, 'report', "
                "'report_jira_progress', %s, %s, 'UTC', %s, %s) returning id",
                (
                    a.world.acme.id,
                    member,
                    f"PAY daily {uuid.uuid4().hex[:6]}",
                    Jsonb({"project": "PAY"}),
                    cron,
                    "channel" if deliver_to else "dm",
                    channel,
                ),
            )[0][0]
        )

    async def sync(self) -> tuple[int, int]:
        return await sync_org(self.agent.stack.pool, self.client, self.settings, self.org)

    async def describe(self, sid: str) -> Any:
        return await self.client.get_schedule_handle(temporal_id(sid)).describe()

    async def next_runs(self, sid: str) -> list[datetime]:
        return [t.astimezone(UTC) for t in (await self.describe(sid)).info.next_action_times[:3]]

    def runs(self, sid: str) -> list[tuple[Any, ...]]:
        return self.agent.sql(  # type: ignore[no-any-return]
            "select status, error, workflow_id from app.tasks where schedule_id = %s order by created_at",
            (sid,),
        )


@pytest_asyncio.fixture(loop_scope="session")
async def live(agent: Agent, stack: Stack) -> AsyncIterator[Live]:
    enable(stack, agent.world.acme, "jira")
    await connect_jira(stack, agent.world.acme, "acc-alice")
    # The world's own schedules are not this module's: never mirror them into Temporal.
    agent.sql("update app.schedules set synced_revision = revision where org_id = %s", (agent.world.acme.id,))
    queue = f"reports-test-{uuid.uuid4().hex[:8]}"  # never the dev stack's queue
    settings = PROVIDER_SETTINGS.model_copy(
        update={"temporal_address": TEMPORAL, "temporal_task_queue": queue, "schedule_jitter_seconds": 0}
    )
    client = await connect_temporal(settings)
    deps = AgentDeps(**{**agent.runner.d.__dict__, "sink_factory": None, "settings": settings})  # real Slack
    activities = SlackActivities(
        SlackDeps(stack.pool, stack.vault, stack.slack, stack.kv, "http://console.test", agent_enabled=True),
        TaskActivities(AgentRunner(deps)),
        ReportActivities(ReportRunner(deps)),
    )
    app = app_for(stack, temporal=client)
    with pytest.MonkeyPatch.context() as env:
        # The API starts workflows with its own settings: this module's queue, never the dev stack's.
        env.setenv("TEMPORAL_TASK_QUEUE", queue)
        env.setenv("SCHEDULE_JITTER_SECONDS", "0")
        get_settings.cache_clear()
        try:
            async with (
                build_worker(client, settings, activities),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://api"
                ) as api,
            ):
                yield Live(agent, client, settings, TaskLive(agent, client, settings, api))
        finally:
            get_settings.cache_clear()
    agent.sql(
        "update app.schedules set status = 'deleted', pause_reason = null where org_id = %s",
        (agent.world.acme.id,),
    )
    await sync_org(stack.pool, client, settings, agent.world.acme.id)  # deletes the Temporal Schedules


def expected(cron: str, timezone: str) -> list[datetime]:
    return [t.astimezone(UTC) for t in Cadence(cron, timezone).next_runs(datetime.now(UTC))]


async def report_dms(after: float) -> list[dict[str, Any]]:
    return [m for m in await channel_messages("U0BOB") if m.get("bot_id") and float(m["ts"]) > after]


# ------------------------------------------------------------------ the Temporal mirror


async def test_schedules_are_mirrored_and_kept_in_sync(live: Live) -> None:
    sid = live.schedule("0 9 * * 1-5")
    assert await live.sync() == (1, 0)
    described = await live.describe(sid)
    assert await live.next_runs(sid) == expected("0 9 * * 1-5", "UTC")  # Temporal agrees with our cadence
    assert described.schedule.spec.time_zone_name == "UTC"
    assert described.schedule.state.paused is False
    assert described.schedule.action.workflow == "ScheduledReportWorkflow"
    assert described.schedule.action.task_queue == live.settings.temporal_task_queue
    assert await live.sync() == (0, 0)  # nothing changed

    live.agent.sql(
        "update app.schedules set cron = '30 8 * * *', timezone = 'Europe/Berlin' where id = %s", (sid,)
    )
    await live.sync()
    assert (await live.describe(sid)).schedule.spec.time_zone_name == "Europe/Berlin"
    assert await live.next_runs(sid) == expected("30 8 * * *", "Europe/Berlin")

    live.agent.sql("update app.schedules set status = 'paused', pause_reason = 'admin' where id = %s", (sid,))
    await live.sync()
    assert (await live.describe(sid)).schedule.state.paused is True
    live.agent.sql("update app.schedules set status = 'active', pause_reason = null where id = %s", (sid,))
    await live.sync()
    assert (await live.describe(sid)).schedule.state.paused is False

    live.agent.sql("update app.schedules set status = 'deleted' where id = %s", (sid,))
    await live.sync()
    with pytest.raises(RPCError):
        await live.describe(sid)
    assert live.agent.sql("select synced_revision = revision from app.schedules where id = %s", (sid,)) == [
        (True,)
    ]


# ------------------------------------------------------------------ runs


async def test_a_triggered_run_delivers_the_report_by_dm(live: Live) -> None:
    sid = live.schedule("0 9 * * *")
    await live.sync()
    before = datetime.now(UTC).timestamp()
    await live.client.get_schedule_handle(temporal_id(sid)).trigger()
    runs = await wait_for(lambda: [r for r in live.runs(sid) if r[0] == "completed"], 60)
    assert runs[0][2].startswith(f"report-{sid}-")
    dms = await wait_for(lambda: report_dms(before), 20)
    text = " ".join(str(b) for m in dms for b in m.get("blocks") or [])
    assert "Created:" in text
    assert "Open now:" in text
    assert live.agent.sql("select last_run_at is not null from app.schedules where id = %s", (sid,)) == [
        (True,)
    ]


async def test_a_schedule_fires_on_time(live: Live) -> None:
    """Due within two minutes, no jitter: it runs at its minute and delivers right after."""
    due = (datetime.now(UTC) + timedelta(minutes=1)).replace(second=0, microsecond=0)
    if (due - datetime.now(UTC)).total_seconds() < 15:  # too close to set up in time
        due += timedelta(minutes=1)
    sid = live.schedule(f"{due.minute} {due.hour} * * *")
    await live.sync()
    before = datetime.now(UTC).timestamp()
    await wait_for(lambda: [r for r in live.runs(sid) if r[0] == "completed"], 150)
    delivered = await wait_for(lambda: report_dms(before), 20)
    late = datetime.fromtimestamp(float(delivered[-1]["ts"]), UTC) - due
    assert timedelta(0) <= late < timedelta(seconds=60), late
    last_run = live.agent.sql("select last_run_at from app.schedules where id = %s", (sid,))[0][0]
    assert last_run == due  # the nominal time: the window is anchored on it


async def test_a_run_of_a_paused_schedule_pauses_its_temporal_schedule(live: Live) -> None:
    sid = live.schedule("0 9 * * *", deliver_to="random")  # the agent is not in #random
    await live.sync()
    await live.client.get_schedule_handle(temporal_id(sid)).trigger()
    row = await wait_for(
        lambda: live.agent.sql(
            "select status, pause_reason, pause_notified_at is not null from app.schedules "
            "where id = %s and pause_notified_at is not null",
            (sid,),
        ),
        60,
    )
    assert row == [("paused", "access_lost", True)]
    await wait_for(lambda: _paused(live, sid), 20)
    assert live.runs(sid) == []  # paused before any task was created


async def _paused(live: Live, sid: str) -> bool:
    try:
        return bool((await live.describe(sid)).schedule.state.paused)
    except RPCError:  # not mirrored yet
        return False


async def test_sync_workflow_mirrors_and_notifies(live: Live) -> None:
    sid = live.schedule("0 9 * * *")
    await live.sync()
    live.agent.sql("update app.schedules set status = 'paused', pause_reason = 'access_lost', "
                   "pause_detail = 'the agent is not in #bugs' where id = %s", (sid,))  # fmt: skip
    before = datetime.now(UTC).timestamp()
    result = await live.client.execute_workflow(
        "ScheduleSyncWorkflow",
        live.org,
        id=f"schedule-sync-test-{uuid.uuid4().hex[:8]}",
        task_queue=live.settings.temporal_task_queue,
    )
    assert "synced" in result
    assert await _paused(live, sid)
    name = live.agent.sql("select name from app.schedules where id = %s", (sid,))[0][0]
    alice = [m for m in await channel_messages("U0ALICE") if float(m["ts"]) > before and name in m["text"]]
    assert len(alice) == 1
    assert "the agent is not in #bugs" in alice[0]["text"]
    await asyncio.sleep(0)


# ------------------------------------------------------------------ member removal, run now


async def test_cancel_tasks_cancels_only_the_members_open_tasks(live: Live) -> None:
    from temporalio.service import RPCStatusCode

    from app.workflows.members import MemberActivities

    a, acme = live.agent, live.agent.world.acme
    quinn = a.sql("select m.id from app.org_memberships m join app.users u on u.id = m.user_id "
                  "where u.email = 'quinn@acme.test'")[0][0]  # fmt: skip
    tag = uuid.uuid4().hex[:8]
    for who, status, wf in ((quinn, "running", f"a-{tag}"), (quinn, "awaiting_approval", f"gone-{tag}"),
                            (quinn, "completed", f"b-{tag}"), (acme.user, "running", f"c-{tag}")):  # fmt: skip
        a.sql(
            "insert into app.tasks (org_id, requested_by, trigger, model_tier, status, workflow_id, finished_at) "
            "values (%s, %s, 'mention', 'standard', %s, %s, case when %s = 'completed' then now() end)",
            (acme.id, who, status, wf, status),
        )
    cancelled: list[str] = []

    class Handle:
        def __init__(self, workflow_id: str) -> None:
            self.id = workflow_id

        async def cancel(self) -> None:
            if self.id.startswith("gone-"):
                raise RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b"")
            cancelled.append(self.id)

    class Stub:
        def get_workflow_handle(self, workflow_id: str) -> Handle:
            return Handle(workflow_id)

    activities = MemberActivities(a.stack.pool, lambda: Stub())  # type: ignore[arg-type, return-value]
    try:
        assert await activities.cancel_tasks(live.org, str(quinn)) == 2
        assert cancelled == [f"a-{tag}"]  # not Bob's, not a finished one; a vanished workflow is fine
    finally:
        a.sql("update app.tasks set status = 'completed', finished_at = now() where workflow_id like %s",
              (f"%-{tag}",))  # fmt: skip


async def test_member_removed_workflow_pauses_and_notifies(live: Live) -> None:
    a = live.agent
    quinn = a.sql("select m.id from app.org_memberships m join app.users u on u.id = m.user_id "
                  "where u.email = 'quinn@acme.test'")[0][0]  # fmt: skip
    sid = live.schedule("0 9 * * *", owner="quinn@acme.test")
    await live.sync()
    name = a.sql("select name from app.schedules where id = %s", (sid,))[0][0]
    before = datetime.now(UTC).timestamp()
    a.sql("update app.org_memberships set status = 'removed' where id = %s", (quinn,))
    try:
        result = await live.client.execute_workflow(
            "MemberRemovedWorkflow",
            args=[live.org, str(quinn)],
            id=f"member-removed-test-{uuid.uuid4().hex[:8]}",
            task_queue=live.settings.temporal_task_queue,
        )
        assert result.startswith("cancelled 0 task(s)")
        assert await _paused(live, sid)
        notices = [
            m for m in await channel_messages("U0ALICE") if float(m["ts"]) > before and name in m["text"]
        ]
        assert len(notices) == 1
        assert "owned by quinn@acme.test" in notices[0]["text"]
    finally:
        a.sql("update app.org_memberships set status = 'active' where id = %s", (quinn,))


async def test_run_now_from_the_console(live: Live, stack: Stack) -> None:
    from app.auth.clerk import ClerkVerifier
    from tests.console.conftest import ISSUER, ORIGIN, Jwks
    from tests.console.test_schedules_api import BOB, KEY, QUINN
    from tests.stack import app_for

    verifier = ClerkVerifier(
        ISSUER, "https://clerk.test/jwks", [ORIGIN],
        http=httpx.AsyncClient(transport=httpx.MockTransport(Jwks([KEY]).handler)),
    )  # fmt: skip
    app = app_for(stack, clerk_verifier=verifier, temporal=live.client)
    sid = live.schedule("0 9 * * *")  # never synced yet: Run now syncs first
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://api"
    ) as console:
        assert (await console.post(f"/api/schedules/{sid}/run", headers=QUINN)).status_code == 404
        before = datetime.now(UTC).timestamp()
        response = await console.post(f"/api/schedules/{sid}/run", headers=BOB)
        assert response.status_code == 202, response.text
    await wait_for(lambda: [r for r in live.runs(sid) if r[0] == "completed"], 60)
    assert await wait_for(lambda: report_dms(before), 20)
    queue = (await live.describe(sid)).schedule.action.task_queue
    assert queue == live.settings.temporal_task_queue


# ------------------------------------------------------------------ from Slack


async def _proposal(live: Live, ts: str) -> dict[str, Any]:
    thread = [m for m in await channel_messages("C0GENERAL") if m.get("thread_ts") == ts and m.get("bot_id")]
    return next(m for m in thread if any(b.get("type") == "actions" for b in m.get("blocks") or []))


def _buttons(message: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for b in message.get("blocks") or [] if b.get("type") == "actions" for e in b["elements"]]


def _active(live: Live, name: str) -> list[tuple[Any, ...]]:
    return live.agent.sql(  # type: ignore[no-any-return]
        "select u.email, s.cron, s.timezone, s.delivery_type, s.params, s.playbook_key from app.schedules s "
        "join app.org_memberships m on m.id = s.owner_membership_id join app.users u on u.id = m.user_id "
        "where s.name = %s and s.status = 'active'",
        (name,),
    )


async def test_a_schedule_requested_in_slack_is_confirmed_by_the_requester(live: Live) -> None:
    event_id, ts = await live.slack.request("every weekday at 9am send me a summary of #bugs")
    assert await live.slack.result(event_id) == "proposed"
    proposal = await _proposal(live, ts)
    buttons = _buttons(proposal)
    assert [b["action_id"] for b in buttons] == ["schedule_confirm", "schedule_cancel"]
    shown = str(proposal["blocks"])
    assert "Weekday #bugs summary" in shown
    assert "Every weekday at 09:00" in shown
    assert "Next runs" in shown
    proposal_id = buttons[0]["value"]
    await live.slack.click("U0QUINN", "schedule_confirm", proposal_id, ts=proposal["ts"])  # not theirs
    assert _active(live, "Weekday #bugs summary") == []
    response = await live.slack.click("U0BOB", "schedule_confirm", proposal_id, ts=proposal["ts"])
    assert response.status_code == 200
    bugs = live.agent.sql("select id::text from app.channels where name = 'bugs' and org_id = %s",
                          (live.agent.world.acme.id,))[0][0]  # fmt: skip
    tz = live.agent.sql("select coalesce((select timezone from app.user_preferences p where p.membership_id = "
                        "%s), 'UTC')", (live.agent.world.acme.user,))[0][0]  # fmt: skip
    assert _active(live, "Weekday #bugs summary") == [
        ("bob@acme.test", "0 9 * * 1-5", tz, "dm", {"channels": [bugs]}, "report_channel_summary")
    ]
    updated = (await _proposal_or_updated(live, ts, proposal["ts"]))["text"]
    assert "✅ Scheduled by <@U0BOB>" in updated
    await live.slack.click("U0BOB", "schedule_confirm", proposal_id, ts=proposal["ts"])  # again
    assert len(_active(live, "Weekday #bugs summary")) == 1
    audit = live.agent.sql("select metadata->>'via' from app.audit_log where action = 'schedule.created' "
                           "and target = 'Weekday #bugs summary'")  # fmt: skip
    assert audit == [("slack",)]


async def _proposal_or_updated(live: Live, thread_ts: str, ts: str) -> dict[str, Any]:
    return next(m for m in await channel_messages("C0GENERAL") if m.get("ts") == ts)


async def test_a_proposal_can_be_cancelled(live: Live) -> None:
    event_id, ts = await live.slack.request("every Monday at 10:30 send me the PAY Jira progress")
    assert await live.slack.result(event_id) == "proposed"
    proposal = await _proposal(live, ts)
    await live.slack.click("U0BOB", "schedule_cancel", _buttons(proposal)[1]["value"], ts=proposal["ts"])
    assert "Cancelled by <@U0BOB>" in (await _proposal_or_updated(live, ts, proposal["ts"]))["text"]
    assert _active(live, "Weekly PAY progress") == []


async def test_a_channel_the_requester_cannot_read_is_refused(live: Live) -> None:
    event_id, ts = await live.slack.request("every day at 8 send me a summary of #leads-private")
    assert await live.slack.result(event_id) == "invalid"
    thread = [m for m in await channel_messages("C0GENERAL") if m.get("thread_ts") == ts and m.get("bot_id")]
    assert any("I can't read #leads-private for you" in m["text"] for m in thread)


async def test_app_home_lists_schedules_and_controls_them(live: Live) -> None:
    sid = live.schedule("0 9 * * *")
    name = live.agent.sql("select name from app.schedules where id = %s", (sid,))[0][0]
    await live.client.execute_workflow(
        "PublishHomeWorkflow",
        HomeRequest(live.org, str(live.agent.world.acme.workspace), "T0ACME", "U0BOB"),
        id=f"home-test-{uuid.uuid4().hex[:8]}",
        task_queue=live.settings.temporal_task_queue,
    )
    home = await _home("U0BOB")
    blocks = home["blocks"]
    assert any(name in str(b.get("text")) for b in blocks)
    controls = next(b for b in blocks if b.get("block_id") == f"schedule:{sid}")
    assert [e["action_id"] for e in controls["elements"]] == [
        "schedule_pause",
        "schedule_run",
        "schedule_delete",
    ]
    await live.slack.click("U0QUINN", "schedule_pause", sid, channel="")  # not theirs, not an admin
    assert live.agent.sql("select status from app.schedules where id = %s", (sid,)) == [("active",)]
    await live.slack.click("U0BOB", "schedule_pause", sid, channel="")
    assert live.agent.sql(
        "select status, pause_reason, pause_detail from app.schedules where id = %s", (sid,)
    ) == [("paused", "admin", "paused by bob@acme.test in Slack")]
    await wait_for(lambda: _paused(live, sid), 20)  # mirrored into Temporal
    await live.slack.click("U0ALICE", "schedule_resume", sid, channel="")  # an admin may
    assert live.agent.sql("select status from app.schedules where id = %s", (sid,)) == [("active",)]
    await live.slack.click("U0BOB", "schedule_delete", sid, channel="")
    assert live.agent.sql("select status from app.schedules where id = %s", (sid,)) == [("deleted",)]


async def _home(user: str) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        data: dict[str, Any] = (
            await client.get(f"{FAKES}/_admin/slack/home", params={"team": "T0ACME", "user": user})
        ).json()
    return data

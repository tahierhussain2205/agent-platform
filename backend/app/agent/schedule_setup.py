"""Scheduling a report from Slack (spec 4.6): the model turns the request into a proposal, code
validates it against the requester's access, and the requester confirms it with a button. The
proposal waits in Key Value for a day; nothing is scheduled before the click.
"""

import json
import logging
import uuid
from datetime import UTC
from typing import Any
from uuid import UUID

from app import kv
from app.agent import prompts
from app.agent.outbox import post_once
from app.agent.playbooks import latest
from app.agent.runner import AgentRunner
from app.agent.types import TaskRef, Understanding
from app.db.tenant import tenant_tx
from app.llm.gateway import BudgetExceededError
from app.schedules.cron import ScheduleError
from app.schedules.service import TITLES, Draft, Valid, validate
from app.slack.adapter import Prompt
from app.tools.base import RefBook

log = logging.getLogger(__name__)
PROPOSAL_TTL = 24 * 3600


def proposal_key(org_id: str | UUID, proposal_id: str) -> str:
    return kv.tenant_key(UUID(str(org_id)), "schedule-proposal", proposal_id)


def summary(valid: Valid, channel_names: dict[str, str], delivery_name: str | None, now: Any) -> str:
    """The proposal as the requester sees it (also the confirmed message)."""
    kind = "channel_summary" if valid.playbook_key == "report_channel_summary" else "jira_progress"
    about = (
        ", ".join(f"#{channel_names.get(c, c)}" for c in valid.params.get("channels") or [])
        if kind == "channel_summary"
        else f"Jira {valid.params.get('project')}"
    )
    runs = ", ".join(t.strftime("%a %b %d %H:%M") for t in valid.cadence.next_runs(now))
    where = f"in #{delivery_name}" if valid.delivery == "channel" else "to you by direct message"
    return (
        f"*{valid.name}*\n"
        f"• Report: {TITLES[kind]} of {about}\n"
        f"• When: {valid.cadence.describe()} ({valid.cadence.timezone})\n"
        f"• Delivered {where}\n"
        f"• Next runs: {runs}"
    )


async def propose_schedule(runner: AgentRunner, ref: TaskRef, u: Understanding) -> str:
    """Returns ``proposed``, ``asked`` (a clarifying question), ``invalid`` or ``stopped``."""
    ctx = await runner._context(ref)
    lines, request_text, _ = await runner._conversation(ref, ctx, RefBook("B"))
    async with tenant_tx(runner.d.pool, ref.org_id) as conn:
        readable = await (
            await conn.execute(
                "select id::text, name, bot_is_member from app.channels where workspace_id = %s and not "
                "archived "
                "and app.can_read_channel(%s, id) order by name",
                (ref.workspace_id, ref.membership_id),
            )
        ).fetchall()
        jira = await (
            await conn.execute("select enabled from app.org_integrations where integration_key = 'jira'")
        ).fetchone()
    names = {name.lower(): cid for cid, name, _ in readable}
    now = runner.d.now()
    local = now.astimezone(runner._zone(ctx.timezone))
    spec = latest("schedule_create")
    try:
        result = await runner.d.gateway.complete(
            runner._call(ref, "schedule"),
            "fast",
            system=prompts.system(ctx.org_name, spec, ctx.roles, prompts.SCHEDULE),
            messages=[
                {
                    "role": "user",
                    "content": f"<request>{request_text}</request>\n<conversation>\n"
                    + "\n".join(lines[-10:])
                    + f"\n</conversation>\n<now>{local.isoformat()}</now>\n"
                    f"<timezone>{ctx.timezone}</timezone>\n"
                    f"<channels>{', '.join('#' + n for _, n, _ in readable)}</channels>\n"
                    f"<jira>{'enabled' if jira and jira[0] else 'not enabled'}</jira>",
                }
            ],
            tools=[prompts.submit_tool("submit_schedule", "Submit the proposed schedule.")],
            tool_choice={"type": "tool", "name": "submit_schedule"},
            max_tokens=600,
        )
    except BudgetExceededError as exc:
        await runner.reply(ref, f"I can't do that now: {exc}.", "schedule-stopped")
        return "stopped"
    raw = result.tool_input("submit_schedule")
    if raw.get("question"):
        await runner.reply(ref, str(raw["question"])[:500], "schedule-question")
        return "asked"
    wanted = [str(c).lstrip("#").lower() for c in raw.get("channels") or []]
    unknown = [c for c in wanted if c not in names]
    delivery_channel = str(raw.get("delivery_channel") or "").lstrip("#").lower() or None
    if delivery_channel and delivery_channel not in names:
        unknown.append(delivery_channel)
    if unknown:
        listed = ", ".join(f"#{c}" for c in unknown)
        await runner.reply(
            ref, f"I can't set that up: I can't read {listed} for you (allowed channels you're a member of).",
            "schedule-invalid",
        )  # fmt: skip
        return "invalid"
    default_name = (
        f"{'Jira ' + str(raw.get('project')) if raw.get('kind') == 'jira_progress' else 'Channel'} report"
    )
    draft = Draft(
        name=str(raw.get("name") or default_name),
        kind=str(raw.get("kind") or ""),
        cron=str(raw.get("cron") or ""),
        timezone=ctx.timezone,
        delivery=str(raw.get("delivery") or "dm"),
        channels=[UUID(names[c]) for c in wanted],
        project=str(raw["project"]) if raw.get("project") else None,
        delivery_channel_id=UUID(names[delivery_channel]) if delivery_channel else None,
    )
    async with tenant_tx(runner.d.pool, ref.org_id) as conn:
        try:
            valid = await validate(conn, UUID(ref.membership_id), draft, now)
        except ScheduleError as exc:
            await runner.reply(ref, f"I can't set that up: {exc.message}", "schedule-invalid")
            return "invalid"
    proposal_id = uuid.uuid4().hex
    stored = {
        "owner": ref.membership_id,
        "slack_user": ref.slack_user_id,
        "task_id": ref.task_id,
        "name": draft.name,
        "kind": draft.kind,
        "cron": valid.cadence.cron,
        "timezone": valid.cadence.timezone,
        "delivery": draft.delivery,
        "channels": [str(c) for c in draft.channels],
        "project": draft.project,
        "delivery_channel_id": str(draft.delivery_channel_id) if draft.delivery_channel_id else None,
    }
    if runner.d.kv is None:
        await runner.reply(
            ref, "I can't schedule reports right now (no Key Value store).", "schedule-invalid"
        )
        return "invalid"
    await runner.d.kv.set(proposal_key(ref.org_id, proposal_id), json.dumps(stored), ex=PROPOSAL_TTL)
    by_id = {cid: name for cid, name, _ in readable}
    delivery_name = by_id.get(str(draft.delivery_channel_id)) if draft.delivery_channel_id else None
    text = (
        summary(valid, by_id, delivery_name, now.astimezone(UTC))
        + "\n_Create it? This proposal expires in a day._"
    )
    intent = Prompt(
        ref.channel,
        text,
        ref.thread_ts or ref.message_ts,
        [("schedule_confirm", "Create schedule", proposal_id), ("schedule_cancel", "Cancel", proposal_id)],
    )
    sink = await runner._sink(ref)
    await post_once(
        runner.d.pool, sink, ref.org_id, ref.task_id, f"task:{ref.task_id}:schedule-proposal", intent,
        channel=ref.channel, thread_ts=ref.thread_ts or ref.message_ts,
    )  # fmt: skip
    log.info("schedule proposed", extra={"task_id": ref.task_id, "kind": draft.kind})
    return "proposed"

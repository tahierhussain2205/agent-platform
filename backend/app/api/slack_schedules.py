"""Slack buttons for scheduled reports: confirming or cancelling a proposal the agent made in a thread
(only the requester), and the App Home controls (the owner or an admin). Saves go through the same
validation as the console; changes are audited, then mirrored into Temporal after the commit."""

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg
from fastapi import Request
from psycopg.types.json import Jsonb

from app.agent.schedule_setup import proposal_key, summary
from app.api.temporal_client import temporal_client
from app.auth.console import resources
from app.db.tenant import tenant_tx
from app.schedules.cron import ScheduleError
from app.schedules.service import KIND_OF, Draft, validate
from app.schedules.temporal import start_sync, sync_org, temporal_id
from app.settings import get_settings
from app.slack import service
from app.slack.client import SlackError
from app.workflows.slack_types import HomeRequest

log = logging.getLogger(__name__)
Tell = Callable[[str], Awaitable[None]]
PROPOSAL_ACTIONS = ("schedule_confirm", "schedule_cancel")
HOME_ACTIONS = ("schedule_pause", "schedule_resume", "schedule_delete", "schedule_run")


async def _clicker(conn: Any, workspace_id: UUID, user: str) -> tuple[UUID, str, str, str] | None:
    """(membership, privilege, status, email) of the Slack user who clicked."""
    row = await (
        await conn.execute(
            "select m.id, m.privilege, m.status, u.email from app.platform_identities pi "
            "join app.org_memberships m on m.id = pi.membership_id join app.users u on u.id = m.user_id "
            "where pi.workspace_id = %s and pi.external_user_id = %s",
            (workspace_id, user),
        )
    ).fetchone()
    return (row[0], row[1], row[2], row[3]) if row else None


async def _update_message(request: Request, org_id: UUID, payload: dict[str, Any], text: str) -> None:
    """Replace the proposal (and its buttons) with the outcome."""
    res = resources(request)
    container = payload.get("container") or {}
    channel, ts = container.get("channel_id"), container.get("message_ts")
    if not (channel and ts and res.db_pool and res.vault and res.slack):
        return
    team, token = await service.bot_token(res.db_pool, res.vault, org_id)
    try:
        await res.slack.call(
            "chat.update",
            token,
            team,
            channel=channel,
            ts=ts,
            text=text[:3000],
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}],
        )
    except SlackError as exc:
        log.warning("could not update the schedule proposal: %s", exc.error)


async def handle(
    request: Request,
    org_id: UUID,
    workspace_id: UUID,
    team_id: str,
    user: str,
    action_id: str,
    value: str,
    payload: dict[str, Any],
    tell: Tell,
) -> None:
    if action_id in PROPOSAL_ACTIONS:
        await _proposal(request, org_id, workspace_id, user, action_id, value, payload, tell)
    elif action_id in HOME_ACTIONS:
        await _home(request, org_id, workspace_id, team_id, user, action_id, value, tell)


async def _proposal(
    request: Request,
    org_id: UUID,
    workspace_id: UUID,
    user: str,
    action_id: str,
    value: str,
    payload: dict[str, Any],
    tell: Tell,
) -> None:
    res = resources(request)
    if res.kv is None or res.db_pool is None or not value.isalnum():
        return
    key = proposal_key(org_id, value)
    raw = await res.kv.get(key)
    if raw is None:
        await tell("This proposal expired or was already handled. Ask me again to schedule it.")
        return
    stored = json.loads(raw)
    if stored.get("slack_user") != user:
        await tell("Only the person who asked can create or cancel this schedule.")
        return
    if await res.kv.getdel(key) is None:  # a second click got here first
        return
    if action_id == "schedule_cancel":
        await _update_message(request, org_id, payload, f"~{stored['name']}~\nCancelled by <@{user}>.")
        return
    draft = Draft(
        stored["name"],
        stored["kind"],
        stored["cron"],
        stored["timezone"],
        stored["delivery"],
        [UUID(c) for c in stored.get("channels") or []],
        stored.get("project"),
        UUID(stored["delivery_channel_id"]) if stored.get("delivery_channel_id") else None,
    )
    owner = UUID(stored["owner"])
    now = datetime.now(UTC)
    try:
        async with tenant_tx(res.db_pool, org_id) as conn:
            valid = await validate(conn, owner, draft, now)  # access may have changed since the proposal
            row = await (
                await conn.execute(
                    "insert into app.schedules (org_id, owner_membership_id, name, instruction, "
                    "playbook_key, "
                    "params, cron, timezone, delivery_type, delivery_channel_id) "
                    "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id",
                    (
                        org_id,
                        owner,
                        valid.name,
                        valid.instruction,
                        valid.playbook_key,
                        Jsonb(valid.params),
                        valid.cadence.cron,
                        valid.cadence.timezone,
                        valid.delivery,
                        valid.delivery_channel_id,
                    ),
                )
            ).fetchone()
            assert row is not None
            await conn.execute(
                "insert into app.audit_log (org_id, actor_type, actor_id, action, target, metadata) "
                "values (%s, 'user', %s, 'schedule.created', %s, %s)",
                (org_id, owner, valid.name, Jsonb({"schedule_id": str(row[0]), "via": "slack"})),
            )
            names = dict(
                await (
                    await conn.execute("select id::text, name from app.channels where org_id = %s", (org_id,))
                ).fetchall()
            )
    except ScheduleError as exc:
        await res.kv.set(key, raw, ex=24 * 3600)  # put it back: it can be fixed and confirmed
        await tell(f"I can't create it: {exc.message}")
        return
    except psycopg.errors.CheckViolation as exc:
        await res.kv.set(key, raw, ex=24 * 3600)
        text = (
            "active schedules" in str(exc) and "The organization has reached its limit of active schedules."
        )
        await tell(text or "I can't create this schedule.")
        return
    await start_sync(await temporal_client(request), get_settings(), org_id)
    delivery = names.get(str(valid.delivery_channel_id)) if valid.delivery_channel_id else None
    text = summary(valid, names, delivery, now)
    await _update_message(
        request, org_id, payload, f"{text}\n✅ Scheduled by <@{user}>. Manage it in App Home or the console."
    )


async def _home(
    request: Request,
    org_id: UUID,
    workspace_id: UUID,
    team_id: str,
    user: str,
    action_id: str,
    value: str,
    tell: Tell,
) -> None:
    res = resources(request)
    if res.db_pool is None:
        return
    try:
        schedule_id = UUID(value)
    except ValueError:
        return
    async with tenant_tx(res.db_pool, org_id) as conn:
        member = await _clicker(conn, workspace_id, user)
        row = await (
            await conn.execute(
                "select name, status, owner_membership_id, playbook_key, params, cron, timezone, "
                "delivery_type, "
                "delivery_channel_id from app.schedules where id = %s and status <> 'deleted'",
                (schedule_id,),
            )
        ).fetchone()
    if row is None or member is None or member[2] != "active":
        await tell("This schedule no longer exists.")
        return
    name, status, owner, playbook_key, params, cron, timezone, delivery, delivery_channel = row
    if not (owner == member[0] or member[1] == "admin"):
        await tell("Only its owner or an admin can change this schedule.")
        return
    settings = get_settings()
    try:
        async with tenant_tx(res.db_pool, org_id) as conn:
            if action_id == "schedule_pause" and status == "active":
                await conn.execute(
                    "update app.schedules set status = 'paused', pause_reason = 'admin', pause_detail = %s "
                    "where id = %s",
                    (f"paused by {member[3]} in Slack", schedule_id),
                )
            elif action_id == "schedule_resume" and status == "paused":
                params = params or {}
                draft = Draft(
                    name,
                    KIND_OF.get(playbook_key or "", ""),
                    cron,
                    timezone,
                    delivery,
                    [UUID(c) for c in params.get("channels") or []],
                    params.get("project"),
                    delivery_channel,
                )
                await validate(conn, owner, draft)
                await conn.execute(
                    "update app.schedules set status = 'active', pause_reason = null where id = %s",
                    (schedule_id,),
                )
            elif action_id == "schedule_delete":
                await conn.execute(
                    "update app.schedules set status = 'deleted', pause_reason = null where id = %s",
                    (schedule_id,),
                )
            elif action_id != "schedule_run":
                return
            await conn.execute(
                "insert into app.audit_log (org_id, actor_type, actor_id, action, target, metadata) "
                "values (%s, 'user', %s, %s, %s, %s)",
                (
                    org_id,
                    member[0],
                    action_id.replace("schedule_", "schedule.").replace(".run", ".run_now"),
                    name,
                    Jsonb({"schedule_id": str(schedule_id), "via": "slack"}),
                ),
            )
    except ScheduleError as exc:
        await tell(f"I can't resume it: {exc.message}")
        return
    except psycopg.errors.CheckViolation:
        await tell("The organization has reached its limit of active schedules.")
        return
    client = await temporal_client(request)
    if action_id == "schedule_run":
        if status != "active" or client is None:
            await tell("Only an active schedule can run.")
            return
        await sync_org(res.db_pool, client, settings, org_id)
        await client.get_schedule_handle(temporal_id(schedule_id)).trigger()
        await tell(f"Running *{name}* now; the report follows shortly.")
    else:
        await start_sync(client, settings, org_id)
    if client is not None:  # refresh the home tab of the person who clicked
        try:
            await client.start_workflow(
                "PublishHomeWorkflow",
                HomeRequest(str(org_id), str(workspace_id), team_id, user),
                id=f"slack-home-schedule-{user}-{uuid.uuid4().hex[:12]}",
                task_queue=settings.temporal_task_queue,
            )
        except Exception:
            log.warning("could not refresh the home tab", exc_info=True)

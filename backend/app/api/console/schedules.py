"""Console: scheduled reports (spec 4.6, 4.7). Members manage their own schedules; admins see
every schedule, pause, resume or delete any, and reassign one whose owner left. Every change is
audited and then mirrored into Temporal after the commit (best effort; the maintenance job and
each run's own checks catch the rest)."""

import logging
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, BackgroundTasks, Depends, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from app.api.console.common import audit, fetch_all, fetch_one
from app.api.errors import ApiError
from app.auth.console import ConsoleIdentity, console_identity, require_admin, resources
from app.db.uow import UnitOfWork, get_uow
from app.schedules.cron import Cadence, ScheduleError, cadence
from app.schedules.service import KIND_OF, Draft, validate
from app.schedules.temporal import start_sync, sync_org, temporal_id
from app.settings import get_settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/schedules")

COLUMNS = """
    s.id, s.name, s.playbook_key, s.params, s.cron, s.timezone, s.delivery_type, s.delivery_channel_id,
    c.name as delivery_channel_name, s.status, s.pause_reason, s.pause_detail, s.paused_at, s.last_run_at,
    s.created_at, s.owner_membership_id, u.email as owner_email, u.name as owner_name,
    m.status as owner_status
"""
FROM = """
    from app.schedules s
    join app.org_memberships m on m.id = s.owner_membership_id
    join app.users u on u.id = m.user_id
    left join app.channels c on c.id = s.delivery_channel_id
"""


def _invalid(exc: ScheduleError) -> ApiError:
    return ApiError(422, exc.code, exc.message)


async def _channel_names(conn: Any) -> dict[str, str]:
    return {str(r["id"]): r["name"] for r in await fetch_all(conn, "select id, name from app.channels")}


def _json(row: dict[str, Any], names: dict[str, str], identity: ConsoleIdentity) -> dict[str, Any]:
    params = row["params"] or {}
    when = Cadence(row["cron"], row["timezone"])
    active = row["status"] == "active"
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "kind": KIND_OF.get(row["playbook_key"] or "", "other"),
        "channels": [{"id": c, "name": names.get(c)} for c in params.get("channels") or []],
        "project": params.get("project"),
        "cron": row["cron"],
        "timezone": row["timezone"],
        "description": when.describe(),
        "next_runs": [t.isoformat() for t in when.next_runs(datetime.now(UTC))] if active else [],
        "delivery": {
            "type": row["delivery_type"],
            "channel_id": str(row["delivery_channel_id"]) if row["delivery_channel_id"] else None,
            "channel_name": row["delivery_channel_name"],
        },
        "status": row["status"],
        "pause_reason": row["pause_reason"],
        "pause_detail": row["pause_detail"],
        "paused_at": row["paused_at"],
        "last_run_at": row["last_run_at"],
        "created_at": row["created_at"],
        "owner": {
            "membership_id": str(row["owner_membership_id"]),
            "email": row["owner_email"],
            "name": row["owner_name"],
            "status": row["owner_status"],
        },
        "can_manage": identity.is_admin or row["owner_membership_id"] == identity.membership_id,
    }


async def _one(conn: Any, schedule_id: UUID, identity: ConsoleIdentity) -> dict[str, Any]:
    """A schedule the caller may manage (the owner or an admin); deleted ones do not exist."""
    row = await fetch_one(
        conn, f"select {COLUMNS} {FROM} where s.id = %s and s.status <> 'deleted'", (schedule_id,)
    )
    if row is None or not (identity.is_admin or row["owner_membership_id"] == identity.membership_id):
        raise ApiError(404, "not_found", "No such schedule.")
    return row


@router.get("")
async def list_schedules(
    identity: ConsoleIdentity = Depends(console_identity), uow: UnitOfWork = Depends(get_uow)
) -> dict[str, Any]:
    conn = uow.connection
    where = "s.status <> 'deleted'" + ("" if identity.is_admin else " and s.owner_membership_id = %s")
    rows = await fetch_all(
        conn,
        f"select {COLUMNS} {FROM} where {where} order by s.status = 'active' desc, s.created_at desc",
        () if identity.is_admin else (identity.membership_id,),
    )
    names = await _channel_names(conn)
    limits = await fetch_one(
        conn,
        "select max_schedules, (select count(*) from app.schedules where status = 'active') as active "
        "from app.org_settings",
    )
    return {"schedules": [_json(r, names, identity) for r in rows], "limits": limits}


@router.get("/options")
async def options(
    identity: ConsoleIdentity = Depends(console_identity), uow: UnitOfWork = Depends(get_uow)
) -> dict[str, Any]:
    """What the caller may schedule: channels they can read, where they can deliver, Jira."""
    conn = uow.connection
    me = identity.membership_id
    channels = await fetch_all(
        conn,
        "select id, name, is_private, bot_is_member from app.channels "
        "where not archived and app.can_read_channel(%s, id) order by name",
        (me,),
    )
    jira = await fetch_one(conn, "select enabled from app.org_integrations where integration_key = 'jira'")
    slack = await fetch_one(
        conn,
        "select exists (select 1 from app.platform_identities pi join app.workspaces w on w.id = "
        "pi.workspace_id "
        "and w.status = 'active' where pi.membership_id = %s) as linked",
        (me,),
    )
    tz = await fetch_one(conn, "select timezone from app.user_preferences where membership_id = %s", (me,))
    return {
        "channels": [
            {"id": str(c["id"]), "name": c["name"], "is_private": c["is_private"]} for c in channels
        ],
        "delivery_channels": [
            {"id": str(c["id"]), "name": c["name"]} for c in channels if c["bot_is_member"]
        ],
        "jira_enabled": bool(jira and jira["enabled"]),
        "has_slack_account": bool(slack and slack["linked"]),
        "timezone": tz["timezone"] if tz else "UTC",
    }


class PreviewBody(BaseModel):
    cron: str = Field(max_length=100)
    timezone: str = Field(max_length=64)


@router.post("/preview")
async def preview(body: PreviewBody, identity: ConsoleIdentity = Depends(console_identity)) -> dict[str, Any]:
    try:
        when = cadence(body.cron, body.timezone)
    except ScheduleError as exc:
        raise _invalid(exc) from exc
    return {
        "cron": when.cron,
        "description": when.describe(),
        "next_runs": [t.isoformat() for t in when.next_runs(datetime.now(UTC), 5)],
    }


class ScheduleBody(BaseModel):
    name: str = Field(max_length=200)
    kind: Literal["channel_summary", "jira_progress"]
    cron: str = Field(max_length=100)
    timezone: str = Field(max_length=64)
    delivery: Literal["dm", "channel"]
    channels: list[UUID] = Field(default_factory=list, max_length=10)
    project: str | None = Field(default=None, max_length=20)
    delivery_channel_id: UUID | None = None

    def draft(self) -> Draft:
        return Draft(
            self.name,
            self.kind,
            self.cron,
            self.timezone,
            self.delivery,
            self.channels,
            self.project,
            self.delivery_channel_id,
        )


def _quota(exc: psycopg.errors.CheckViolation) -> ApiError | None:
    if "active schedules" in str(exc):
        return ApiError(409, "schedule_limit", "The organization has reached its limit of active schedules.")
    return None


def _sync_later(background: BackgroundTasks, request: Request, org_id: UUID) -> None:
    background.add_task(start_sync, resources(request).temporal, get_settings(), org_id)


@router.post("", status_code=201)
async def create(
    body: ScheduleBody,
    request: Request,
    background: BackgroundTasks,
    identity: ConsoleIdentity = Depends(console_identity),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    conn = uow.connection
    try:
        valid = await validate(conn, identity.membership_id, body.draft())
    except ScheduleError as exc:
        raise _invalid(exc) from exc
    try:
        async with conn.transaction():
            row = await fetch_one(
                conn,
                "insert into app.schedules (org_id, owner_membership_id, name, instruction, "
                "playbook_key, params, "
                "cron, timezone, delivery_type, delivery_channel_id) values (%s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s) "
                "returning id",
                (
                    identity.org_id,
                    identity.membership_id,
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
    except psycopg.errors.CheckViolation as exc:
        raise _quota(exc) or ApiError(422, "invalid", "The schedule is not valid.") from exc
    assert row is not None
    await audit(conn, identity, "schedule.created", valid.name, {"schedule_id": str(row["id"])})
    _sync_later(background, request, identity.org_id)
    return _json(await _one(conn, row["id"], identity), await _channel_names(conn), identity)


class UpdateBody(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    kind: Literal["channel_summary", "jira_progress"] | None = None
    cron: str | None = Field(default=None, max_length=100)
    timezone: str | None = Field(default=None, max_length=64)
    delivery: Literal["dm", "channel"] | None = None
    channels: list[UUID] | None = Field(default=None, max_length=10)
    project: str | None = Field(default=None, max_length=20)
    delivery_channel_id: UUID | None = None


@router.patch("/{schedule_id}")
async def update(
    schedule_id: UUID,
    body: UpdateBody,
    request: Request,
    background: BackgroundTasks,
    identity: ConsoleIdentity = Depends(console_identity),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    """Validated against the owner's access, whoever edits it."""
    conn = uow.connection
    row = await _one(conn, schedule_id, identity)
    params = row["params"] or {}
    fields = body.model_fields_set
    draft = Draft(
        body.name if body.name is not None else row["name"],
        body.kind or KIND_OF.get(row["playbook_key"] or "", ""),
        body.cron or row["cron"],
        body.timezone or row["timezone"],
        body.delivery or row["delivery_type"],
        body.channels if body.channels is not None else [UUID(c) for c in params.get("channels") or []],
        body.project if "project" in fields else params.get("project"),
        body.delivery_channel_id if "delivery_channel_id" in fields else row["delivery_channel_id"],
    )
    if draft.delivery == "dm":
        draft.delivery_channel_id = None
    try:
        valid = await validate(conn, row["owner_membership_id"], draft)
    except ScheduleError as exc:
        raise _invalid(exc) from exc
    await conn.execute(
        "update app.schedules set name = %s, instruction = %s, playbook_key = %s, params = %s, cron = %s, "
        "timezone = %s, delivery_type = %s, delivery_channel_id = %s where id = %s",
        (
            valid.name,
            valid.instruction,
            valid.playbook_key,
            Jsonb(valid.params),
            valid.cadence.cron,
            valid.cadence.timezone,
            valid.delivery,
            valid.delivery_channel_id,
            schedule_id,
        ),
    )
    await audit(conn, identity, "schedule.updated", valid.name, {"schedule_id": str(schedule_id)})
    _sync_later(background, request, identity.org_id)
    return _json(await _one(conn, schedule_id, identity), await _channel_names(conn), identity)


@router.post("/{schedule_id}/pause")
async def pause(
    schedule_id: UUID,
    request: Request,
    background: BackgroundTasks,
    identity: ConsoleIdentity = Depends(console_identity),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    conn = uow.connection
    row = await _one(conn, schedule_id, identity)
    if row["status"] != "active":
        raise ApiError(409, "not_active", "The schedule is not active.")
    me = await fetch_one(conn, "select email from app.users where id = %s", (identity.user_id,))
    await conn.execute(
        "update app.schedules set status = 'paused', pause_reason = 'admin', pause_detail = %s where id = %s",
        (f"paused by {me['email'] if me else 'a person'}", schedule_id),
    )
    await audit(conn, identity, "schedule.paused", row["name"], {"schedule_id": str(schedule_id)})
    _sync_later(background, request, identity.org_id)
    return _json(await _one(conn, schedule_id, identity), await _channel_names(conn), identity)


@router.post("/{schedule_id}/resume")
async def resume(
    schedule_id: UUID,
    request: Request,
    background: BackgroundTasks,
    identity: ConsoleIdentity = Depends(console_identity),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    """The owner's access is checked again (it may be why it was paused); the quota applies."""
    conn = uow.connection
    row = await _one(conn, schedule_id, identity)
    if row["status"] != "paused":
        raise ApiError(409, "not_paused", "The schedule is not paused.")
    params = row["params"] or {}
    draft = Draft(
        row["name"],
        KIND_OF.get(row["playbook_key"] or "", ""),
        row["cron"],
        row["timezone"],
        row["delivery_type"],
        [UUID(c) for c in params.get("channels") or []],
        params.get("project"),
        row["delivery_channel_id"],
    )
    try:
        await validate(conn, row["owner_membership_id"], draft)
    except ScheduleError as exc:
        raise _invalid(exc) from exc
    try:
        async with conn.transaction():
            await conn.execute(
                "update app.schedules set status = 'active', pause_reason = null where id = %s",
                (schedule_id,),
            )
    except psycopg.errors.CheckViolation as exc:
        raise _quota(exc) or ApiError(422, "invalid", "The schedule cannot be resumed.") from exc
    await audit(conn, identity, "schedule.resumed", row["name"], {"schedule_id": str(schedule_id)})
    _sync_later(background, request, identity.org_id)
    return _json(await _one(conn, schedule_id, identity), await _channel_names(conn), identity)


@router.delete("/{schedule_id}", status_code=204)
async def delete(
    schedule_id: UUID,
    request: Request,
    background: BackgroundTasks,
    identity: ConsoleIdentity = Depends(console_identity),
    uow: UnitOfWork = Depends(get_uow),
) -> None:
    conn = uow.connection
    row = await _one(conn, schedule_id, identity)
    await conn.execute(
        "update app.schedules set status = 'deleted', pause_reason = null where id = %s", (schedule_id,)
    )
    await audit(conn, identity, "schedule.deleted", row["name"], {"schedule_id": str(schedule_id)})
    _sync_later(background, request, identity.org_id)


class ReassignBody(BaseModel):
    owner_membership_id: UUID


@router.post("/{schedule_id}/reassign")
async def reassign(
    schedule_id: UUID,
    body: ReassignBody,
    request: Request,
    background: BackgroundTasks,
    identity: ConsoleIdentity = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    """A new owner for a schedule (typically one whose owner left). It stays paused if it was: resuming
    checks the new owner's access."""
    conn = uow.connection
    row = await _one(conn, schedule_id, identity)
    owner = await fetch_one(
        conn,
        "select m.status, u.email from app.org_memberships m join app.users u on u.id = m.user_id where "
        "m.id = %s",
        (body.owner_membership_id,),
    )
    if owner is None or owner["status"] != "active":
        raise ApiError(422, "owner_inactive", "The new owner must be an active member.")
    await conn.execute(
        "update app.schedules set owner_membership_id = %s where id = %s",
        (body.owner_membership_id, schedule_id),
    )
    await audit(
        conn,
        identity,
        "schedule.reassigned",
        row["name"],
        {"schedule_id": str(schedule_id), "to": owner["email"]},
    )
    _sync_later(background, request, identity.org_id)
    return _json(await _one(conn, schedule_id, identity), await _channel_names(conn), identity)


@router.post("/{schedule_id}/run", status_code=202)
async def run_now(
    schedule_id: UUID,
    request: Request,
    identity: ConsoleIdentity = Depends(console_identity),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, str]:
    """Run an active schedule now (the report covers the period since its previous scheduled run)."""
    row = await _one(uow.connection, schedule_id, identity)
    if row["status"] != "active":
        raise ApiError(409, "not_active", "Only an active schedule can run.")
    res = resources(request)
    if res.temporal is None or res.db_pool is None:
        raise ApiError(503, "unavailable", "Schedules cannot run right now.")
    settings = get_settings()
    await sync_org(res.db_pool, res.temporal, settings, identity.org_id)  # it may be brand new
    try:
        await res.temporal.get_schedule_handle(temporal_id(schedule_id)).trigger()
    except Exception as exc:
        log.warning("could not trigger a schedule", extra={"schedule_id": str(schedule_id)}, exc_info=True)
        raise ApiError(503, "unavailable", "The schedule could not be started. Try again.") from exc
    await audit(uow.connection, identity, "schedule.run_now", row["name"], {"schedule_id": str(schedule_id)})
    return {"status": "started"}

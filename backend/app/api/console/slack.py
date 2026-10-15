"""Console: Slack connection, channels and the allowlist, access requests."""

import secrets
import uuid
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.api.console.common import audit, fetch_all, fetch_one
from app.api.errors import ApiError
from app.auth.console import ConsoleIdentity, console_identity, require_admin, resources
from app.db.uow import UnitOfWork, get_uow
from app.identity.invite import InviteError, invite_member
from app.settings import get_settings
from app.slack import service
from app.slack.client import SlackError
from app.workflows.slack_types import SyncRequest

router = APIRouter(prefix="/api")


async def _workspace(conn: Any) -> dict[str, Any] | None:
    return await fetch_one(
        conn,
        "select id, external_id, name, bot_user_id, installed_at from app.workspaces where status = 'active'",
    )


async def _start(request: Request, workflow: str, arg: Any, workflow_id: str) -> None:
    from app.api.slack import start

    try:
        await start(request, workflow, arg, workflow_id)
    except Exception as exc:
        raise ApiError(503, "workflows_unavailable", "Could not start the sync. Try again.") from exc


@router.get("/slack")
async def slack_status(
    identity: ConsoleIdentity = Depends(console_identity), uow: UnitOfWork = Depends(get_uow)
) -> dict[str, Any]:
    settings = get_settings()
    workspace = await _workspace(uow.connection)
    channels: list[dict[str, Any]] = []
    if workspace:
        channels = await fetch_all(
            uow.connection,
            """
            select c.id, c.external_id, c.name, c.is_private, c.is_allowlisted, c.bot_is_member, c.archived,
                   (select count(*) from app.channel_members cm where cm.channel_id = c.id) as members,
                   (select count(*) from app.schedules s
                     where s.delivery_channel_id = c.id and s.status = 'active') as schedules
              from app.channels c
             where c.workspace_id = %s
             order by c.archived, c.is_allowlisted desc, lower(c.name)
            """,
            (workspace["id"],),
        )
    return {
        "workspace": workspace,
        "channels": channels,
        "install_configured": bool(settings.slack_client_id and settings.slack_state_secret),
    }


@router.get("/slack/install")
async def install_url(
    identity: ConsoleIdentity = Depends(require_admin), uow: UnitOfWork = Depends(get_uow)
) -> dict[str, str]:
    settings = get_settings()
    if not settings.slack_client_id or settings.slack_state_secret is None:
        raise ApiError(503, "slack_not_configured", "The Slack app is not configured for this environment.")
    state = service.make_state(
        settings.slack_state_secret.get_secret_value(),
        identity.org_id,
        identity.membership_id,
        secrets.token_urlsafe(16),
    )
    params = {
        "client_id": settings.slack_client_id,
        "scope": ",".join(service.BOT_SCOPES),
        "redirect_uri": f"{settings.api_public_url}/slack/oauth/callback",
        "state": state,
    }
    await audit(uow.connection, identity, "slack.install_started")
    return {"url": f"{settings.slack_authorize_url}?{urlencode(params)}"}


@router.post("/slack/sync", status_code=202)
async def sync(
    request: Request, identity: ConsoleIdentity = Depends(require_admin), uow: UnitOfWork = Depends(get_uow)
) -> dict[str, str]:
    workspace = await _workspace(uow.connection)
    if workspace is None:
        raise ApiError(409, "slack_not_connected", "Connect Slack first.")
    workflow_id = f"slack-sync-console-{workspace['id']}-{uuid.uuid4().hex[:8]}"
    await _start(
        request,
        "SyncWorkspaceWorkflow",
        SyncRequest(str(identity.org_id), str(workspace["id"]), workspace["external_id"]),
        workflow_id,
    )
    await audit(uow.connection, identity, "slack.sync_started")
    return {"workflow_id": workflow_id}


class ChannelUpdate(BaseModel):
    is_allowlisted: bool


@router.patch("/slack/channels/{channel_id}")
async def update_channel(
    channel_id: UUID,
    body: ChannelUpdate,
    request: Request,
    identity: ConsoleIdentity = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    conn = uow.connection
    channel = await fetch_one(
        conn,
        "select id, name, external_id, workspace_id, bot_is_member, archived, is_allowlisted from "
        "app.channels where id = %s",
        (channel_id,),
    )
    if channel is None:
        raise ApiError(404, "not_found", "No such channel.")
    if channel["archived"] and body.is_allowlisted:
        raise ApiError(409, "archived", "Archived channels cannot be allowlisted.")
    paused = 0
    if channel["is_allowlisted"] != body.is_allowlisted:
        before = await fetch_one(
            conn,
            "select count(*) as n from app.schedules where delivery_channel_id = %s and status = 'active'",
            (channel_id,),
        )
        # Removing a channel from the allowlist pauses schedules delivering there (database trigger).
        await conn.execute(
            "update app.channels set is_allowlisted = %s where id = %s", (body.is_allowlisted, channel_id)
        )
        after = await fetch_one(
            conn,
            "select count(*) as n from app.schedules where delivery_channel_id = %s and status = 'active'",
            (channel_id,),
        )
        paused = int(before["n"]) - int(after["n"]) if before and after else 0
        await audit(
            conn,
            identity,
            "slack.channel_allowlisted" if body.is_allowlisted else "slack.channel_removed",
            channel["name"],
            {"paused_schedules": paused},
        )
        if body.is_allowlisted and channel["bot_is_member"]:
            workspace = await _workspace(conn)
            if workspace is not None:
                sync = SyncRequest(
                    str(identity.org_id), str(workspace["id"]), workspace["external_id"], [str(channel_id)]
                )
                await _start(
                    request,
                    "SyncWorkspaceWorkflow",
                    sync,
                    f"slack-sync-channel-{channel_id}-{uuid.uuid4().hex[:8]}",
                )
    return {"id": channel_id, "is_allowlisted": body.is_allowlisted, "paused_schedules": paused}


@router.delete("/slack", status_code=204)
async def disconnect(
    request: Request, identity: ConsoleIdentity = Depends(require_admin), uow: UnitOfWork = Depends(get_uow)
) -> None:
    res = resources(request)
    workspace = await _workspace(uow.connection)
    if workspace is None:
        raise ApiError(409, "slack_not_connected", "Slack is not connected.")
    assert res.vault is not None
    assert res.slack is not None
    assert res.db_pool is not None
    try:
        team, token = await service.bot_token(res.db_pool, res.vault, identity.org_id)
        await res.slack.call("auth.revoke", token, team)
    except (service.SlackNotConnectedError, SlackError, LookupError):
        pass  # already gone on Slack's side: still disconnect here
    await service.uninstall(
        uow.connection,
        res.vault,
        identity.org_id,
        workspace["external_id"],
        reason="disconnected in the console",
        actor=identity.membership_id,
    )


# ------------------------------------------------------------------ access requests


@router.post("/access-requests/{request_id}/invite")
async def invite_requester(
    request_id: UUID,
    request: Request,
    identity: ConsoleIdentity = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    conn = uow.connection
    target = await fetch_one(
        conn, "select id, email, status from app.access_requests where id = %s", (request_id,)
    )
    if target is None:
        raise ApiError(404, "not_found", "No such access request.")
    if target["status"] != "pending":
        raise ApiError(409, "already_decided", "This request was already handled.")
    if not target["email"]:
        raise ApiError(
            422, "no_email", "No email is known for this person; invite them by email on the Members page."
        )
    res = resources(request)
    if res.identity_pool is None or res.clerk_client is None:
        raise ApiError(503, "identity_unavailable", "Identity management is not available.")
    try:
        membership_id = await invite_member(
            res.identity_pool,
            res.clerk_client,
            identity.clerk_org_id,
            target["email"],
            "user",
            identity.clerk_user_id,
        )
    except InviteError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc
    await conn.execute(
        "update app.access_requests set status = 'approved', resolved_by = %s, resolved_at = now() where "
        "id = %s",
        (identity.membership_id, request_id),
    )
    await audit(conn, identity, "access_request.approved", target["email"])
    return {"id": request_id, "status": "approved", "membership_id": membership_id}


@router.post("/access-requests/{request_id}/dismiss")
async def dismiss_requester(
    request_id: UUID, identity: ConsoleIdentity = Depends(require_admin), uow: UnitOfWork = Depends(get_uow)
) -> dict[str, Any]:
    row = await fetch_one(
        uow.connection,
        "update app.access_requests set status = 'dismissed', resolved_by = %s, resolved_at = now() "
        "where id = %s and status = 'pending' returning email, external_user_id",
        (identity.membership_id, request_id),
    )
    if row is None:
        raise ApiError(409, "already_decided", "This request was already handled or does not exist.")
    await audit(uow.connection, identity, "access_request.dismissed", row["email"] or row["external_user_id"])
    return {"id": request_id, "status": "dismissed"}

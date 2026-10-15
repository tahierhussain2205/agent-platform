"""Slack ingress (spec 14.2): events, interactions, OAuth install callback.

Ingress verifies, deduplicates, resolves the org, enqueues and acknowledges. It never does agent
work: mentions and DMs start a Temporal workflow whose id derives from the event id (Temporal's
duplicate rejection is the second dedupe layer). Only references go into the workflow, never
message text. Channel and membership events are small idempotent writes done inline.
"""

import json
import logging
from typing import Any
from urllib.parse import parse_qs, urlencode
from uuid import UUID, uuid4

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from app import kv
from app.auth.console import resources
from app.db.tenant import tenant_tx
from app.identity.invite import InviteError, invite_member
from app.settings import get_settings
from app.slack import service, signature
from app.slack.adapter import ChannelAdapter, DirectMessage, Ephemeral, Intent
from app.slack.client import SlackClient, SlackError
from app.temporal import connect_temporal
from app.vault import Vault
from app.workflows.slack_types import HomeRequest, SlackRequest, SyncRequest

log = logging.getLogger(__name__)
router = APIRouter()

CLAIM_TTL = 60  # an event being processed
DONE_TTL = 3600  # Slack retries within minutes; keep the marker a while longer
MAX_BODY_BYTES = 1024 * 1024
AGENT_EVENTS = {"app_mention", "message"}
CHANNEL_EVENTS = {
    "member_joined_channel",
    "member_left_channel",
    "channel_created",
    "channel_rename",
    "channel_archive",
    "channel_unarchive",
    "channel_deleted",
    "group_rename",
    "group_archive",
    "group_unarchive",
    "group_deleted",
}


def ok() -> Response:
    return Response(status_code=200)


async def temporal(request: Request) -> Client:
    res = resources(request)
    if res.temporal is None:
        res.temporal = await connect_temporal(get_settings(), attempts=2, delay=0.2)
    client: Client = res.temporal
    return client


async def start(request: Request, workflow: str, arg: Any, workflow_id: str) -> None:
    client = await temporal(request)
    try:
        await client.start_workflow(
            workflow, arg, id=workflow_id, task_queue=get_settings().temporal_task_queue
        )
    except WorkflowAlreadyStartedError:
        log.info("workflow already started", extra={"workflow_id": workflow_id})


async def _verified_body(request: Request) -> bytes | Response:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
        return JSONResponse({"error": "too_large"}, status_code=413)
    body = await request.body()
    secret = get_settings().slack_signing_secret
    if secret is None:
        return JSONResponse({"error": "not_configured"}, status_code=503)
    try:
        signature.verify(
            secret.get_secret_value(),
            request.headers.get("x-slack-request-timestamp"),
            request.headers.get("x-slack-signature"),
            body,
        )
    except signature.SlackSignatureError as exc:
        log.warning("rejected slack request: %s", exc)
        return JSONResponse({"error": "invalid_signature"}, status_code=401)
    return body


@router.post("/slack/events", include_in_schema=False)
async def events(request: Request) -> Response:
    body = await _verified_body(request)
    if isinstance(body, Response):
        return body
    try:
        payload: dict[str, Any] = json.loads(body)
    except ValueError:
        return JSONResponse({"error": "bad_payload"}, status_code=400)
    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload.get("challenge")})
    if payload.get("type") != "event_callback":
        return ok()

    res = resources(request)
    event: dict[str, Any] = payload.get("event") or {}
    event_id = str(payload.get("event_id", ""))
    team_id = str(payload.get("team_id") or event.get("team") or "")
    claim_key = kv.global_key("slack-event", event_id or "none")
    if res.kv is not None and event_id and not await kv.claim(res.kv, claim_key, CLAIM_TTL, "processing"):
        return ok()  # a retry of an event we already have

    try:
        outcome = await _route(request, payload, event, event_id, team_id)
    except Exception:
        log.exception("slack event failed", extra={"event_type": event.get("type"), "event_id": event_id})
        if res.kv is not None and event_id:
            await kv.release(res.kv, claim_key)
        return JSONResponse({"error": "failed"}, status_code=500)  # Slack retries
    if res.kv is not None and event_id:
        await res.kv.set(claim_key, "done", ex=DONE_TTL)
    log.info("slack event %s", outcome, extra={"event_type": event.get("type"), "event_id": event_id})
    return ok()


async def _route(
    request: Request, payload: dict[str, Any], event: dict[str, Any], event_id: str, team_id: str
) -> str:
    res = resources(request)
    assert res.db_pool is not None
    async with res.db_pool.connection() as conn:
        row = await (
            await conn.execute(
                "select org_id, workspace_id from app.resolve_workspace('slack', %s)", (team_id,)
            )
        ).fetchone()
    if row is None:
        return "unknown_workspace"  # 200 so Slack stops retrying
    org_id, workspace_id = row
    kind = str(event.get("type", ""))

    if kind in ("app_uninstalled", "tokens_revoked"):
        if kind == "tokens_revoked" and not (event.get("tokens") or {}).get("bot"):
            return "user_tokens_ignored"
        assert res.vault is not None
        async with tenant_tx(res.db_pool, org_id) as conn:
            await service.uninstall(conn, res.vault, org_id, team_id, reason=kind)
        return "uninstalled"

    if kind in CHANNEL_EVENTS:
        async with tenant_tx(res.db_pool, org_id) as conn:
            bot = await (
                await conn.execute("select bot_user_id from app.workspaces where id = %s", (workspace_id,))
            ).fetchone()
            return await service.apply_channel_event(
                conn, res.kv, org_id, workspace_id, bot[0] if bot else None, event
            )

    if kind == "app_home_opened":
        if event.get("tab") != "home":
            return "ignored"
        await start(
            request,
            "PublishHomeWorkflow",
            HomeRequest(str(org_id), str(workspace_id), team_id, str(event["user"])),
            f"slack-home-{event_id}",
        )
        return "home"

    if kind in AGENT_EVENTS:
        if kind == "message" and (
            event.get("channel_type") != "im" or event.get("subtype") or event.get("bot_id")
        ):
            return "ignored"  # only plain user DMs; edits, joins and bot messages are not requests
        if event.get("bot_id") or not event.get("user"):
            return "ignored"
        channel = str(event.get("channel", ""))
        channel_type = "im" if kind == "message" else ("group" if channel.startswith("G") else "channel")
        request_ref = SlackRequest(
            org_id=str(org_id),
            workspace_id=str(workspace_id),
            team_id=team_id,
            channel=channel,
            channel_type=channel_type,
            user=str(event["user"]),
            ts=str(event.get("ts", "")),
            thread_ts=event.get("thread_ts"),
            event_id=event_id,
            event_type=kind,
        )
        await start(request, "AgentRequestWorkflow", request_ref, f"slack-{event_id}")
        return "enqueued"
    return "ignored"


# ------------------------------------------------------------------ interactions


@router.post("/slack/interactions", include_in_schema=False)
async def interactions(request: Request) -> Response:
    body = await _verified_body(request)
    if isinstance(body, Response):
        return body
    try:
        payload: dict[str, Any] = json.loads(parse_qs(body.decode())["payload"][0])
    except (KeyError, IndexError, ValueError):
        return JSONResponse({"error": "bad_payload"}, status_code=400)
    if payload.get("type") != "block_actions" or not payload.get("actions"):
        return ok()
    res = resources(request)
    assert res.db_pool is not None
    team_id = str((payload.get("team") or {}).get("id", ""))
    async with res.db_pool.connection() as conn:
        row = await (
            await conn.execute(
                "select org_id, workspace_id from app.resolve_workspace('slack', %s)", (team_id,)
            )
        ).fetchone()
    if row is None:
        return ok()
    org_id, workspace_id = row
    user = str((payload.get("user") or {}).get("id", ""))
    action = payload["actions"][0]
    action_id = str(action.get("action_id", ""))
    try:
        if action_id == "home_mode":
            await _set_mode(
                request,
                org_id,
                workspace_id,
                team_id,
                user,
                str((action.get("selected_option") or {}).get("value", "")),
            )
        elif action_id == "home_request_access":
            ref = SlackRequest(
                str(org_id),
                str(workspace_id),
                team_id,
                user,
                "im",
                user,
                str(action.get("action_ts", "")),
                None,
                f"home-{user}-{action.get('action_ts')}",
                "home_request",
            )
            await start(
                request, "AgentRequestWorkflow", ref, f"slack-home-request-{user}-{action.get('action_ts')}"
            )
        elif action_id in ("access_request_invite", "access_request_dismiss"):
            await _decide_access_request(
                request, org_id, workspace_id, team_id, user, action_id, str(action.get("value", "")), payload
            )
        elif action_id in ("approval_approve", "approval_reject"):
            await _decide_approval(
                request, org_id, workspace_id, user, action_id, str(action.get("value", "")), payload
            )
        elif action_id == "task_cancel":
            await _cancel_task(request, org_id, workspace_id, user, str(action.get("value", "")), payload)
    except Exception:
        log.exception("slack interaction failed", extra={"action_id": action_id})
        return JSONResponse({"error": "failed"}, status_code=500)
    return ok()


async def _member(conn: Any, workspace_id: UUID, user: str) -> tuple[UUID, str, str, str | None] | None:
    row = await (
        await conn.execute(
            "select m.id, m.privilege, m.status, u.clerk_user_id from app.platform_identities pi "
            "join app.org_memberships m on m.id = pi.membership_id join app.users u on u.id = m.user_id "
            "where pi.workspace_id = %s and pi.external_user_id = %s",
            (workspace_id, user),
        )
    ).fetchone()
    return (row[0], row[1], row[2], row[3]) if row else None


async def _set_mode(
    request: Request, org_id: UUID, workspace_id: UUID, team_id: str, user: str, mode: str
) -> None:
    if mode not in ("ask", "auto"):
        return
    res = resources(request)
    assert res.db_pool is not None
    async with tenant_tx(res.db_pool, org_id) as conn:
        member = await _member(conn, workspace_id, user)
        if member is None or member[2] not in ("active", "invited"):
            return
        await conn.execute(
            "insert into app.user_preferences (membership_id, org_id, mode) values (%s, %s, %s) "
            "on conflict (membership_id) do update set mode = excluded.mode",
            (member[0], org_id, mode),
        )
        await conn.execute(
            "insert into app.audit_log (org_id, actor_type, actor_id, action, target) values (%s, "
            "'user', %s, 'preferences.mode_changed', %s)",
            (org_id, member[0], mode),
        )
    await start(
        request,
        "PublishHomeWorkflow",
        HomeRequest(str(org_id), str(workspace_id), team_id, user),
        f"slack-home-mode-{user}-{uuid4().hex[:8]}",
    )


async def _decide_access_request(
    request: Request,
    org_id: UUID,
    workspace_id: UUID,
    team_id: str,
    user: str,
    action_id: str,
    request_id: str,
    payload: dict[str, Any],
) -> None:
    res = resources(request)
    assert res.db_pool is not None
    assert res.vault is not None
    assert res.slack is not None
    async with tenant_tx(res.db_pool, org_id) as conn:
        actor = await _member(conn, workspace_id, user)
        target = await (
            await conn.execute(
                "select email, external_user_id, status from app.access_requests where id = %s", (request_id,)
            )
        ).fetchone()
        org = await (await conn.execute("select clerk_org_id from app.orgs")).fetchone()
    team, token = await service.bot_token(res.db_pool, res.vault, org_id)
    adapter = ChannelAdapter(res.slack, res.kv)
    if actor is None or actor[1] != "admin" or actor[2] != "active":
        await adapter.send(team, token, DirectMessage(user, "Only active admins can decide access requests."))
        return
    if target is None or target[2] != "pending":
        await _update_decision_message(res, team, token, payload, "This request was already handled.")
        return
    email, external_user, _ = target
    if action_id == "access_request_invite":
        if not email:
            await _update_decision_message(
                res, team, token, payload, "No email is known for this person; invite them from the console."
            )
            return
        if res.identity_pool is None or res.clerk_client is None or org is None:
            raise RuntimeError("identity management is not configured")
        try:
            await invite_member(res.identity_pool, res.clerk_client, org[0], email, "user", actor[3])
        except InviteError as exc:
            await _update_decision_message(
                res, team, token, payload, f"Could not invite {email}: {exc.message}"
            )
            return
        status, note = "approved", f"<@{user}> invited {email}. They can use the agent right away."
    else:
        status, note = "dismissed", f"<@{user}> dismissed the request from <@{external_user}>."
    async with tenant_tx(res.db_pool, org_id) as conn:
        await conn.execute(
            "update app.access_requests set status = %s, resolved_by = %s, resolved_at = now() where id "
            "= %s and status = 'pending'",
            (status, actor[0], request_id),
        )
        await conn.execute(
            "insert into app.audit_log (org_id, actor_type, actor_id, action, target) values (%s, "
            "'user', %s, %s, %s)",
            (org_id, actor[0], f"access_request.{status}", email or external_user),
        )
    await _update_decision_message(res, team, token, payload, note)


async def _update_decision_message(
    res: Any, team: str, token: str, payload: dict[str, Any], text: str
) -> None:
    container = payload.get("container") or {}
    channel, ts = container.get("channel_id"), container.get("message_ts")
    if not (channel and ts):
        return
    try:
        await res.slack.call(
            "chat.update",
            token,
            team,
            channel=channel,
            ts=ts,
            text=text,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
        )
    except SlackError as exc:
        log.warning("could not update the decision message: %s", exc.error)


# ------------------------------------------------------------------ approvals and cancellation


def _required(request: Request) -> tuple[Any, Vault, SlackClient]:
    res = resources(request)
    if res.db_pool is None or res.vault is None or res.slack is None:
        raise RuntimeError("database, vault and Slack must be configured")
    return res.db_pool, res.vault, res.slack


async def _tell_clicker(
    request: Request, org_id: UUID, user: str, payload: dict[str, Any], text: str
) -> None:
    """A private reply to the person who clicked, where they clicked (or a DM)."""
    pool, vault, slack = _required(request)
    team, token = await service.bot_token(pool, vault, org_id)
    container = payload.get("container") or {}
    channel = container.get("channel_id") or (payload.get("channel") or {}).get("id")
    intents: list[Intent] = [Ephemeral(channel, user, text, container.get("thread_ts"))] if channel else []
    for intent in [*intents, DirectMessage(user, text)]:
        try:
            await ChannelAdapter(slack, resources(request).kv).send(team, token, intent)
            return
        except SlackError as exc:
            log.info("private reply not delivered: %s", exc.error)


async def _decide_approval(
    request: Request, org_id: UUID, workspace_id: UUID, user: str, action_id: str, value: str,
    payload: dict[str, Any],
) -> None:  # fmt: skip
    """Write the decision (the database trigger enforces who may decide, expiry and finality),
    then signal the waiting task and update the approval message."""
    pool, vault, slack = _required(request)
    decision = "approved" if action_id == "approval_approve" else "rejected"
    try:
        approval_id = UUID(value)
    except ValueError:
        return
    async with tenant_tx(pool, org_id) as conn:
        member = await _member(conn, workspace_id, user)
        row = await (
            await conn.execute(
                "select a.status, a.message_ref, a.action_summary, t.workflow_id, a.expires_at <= now(), "
                "a.task_id from app.approvals a join app.tasks t on t.id = a.task_id where a.id = %s",
                (approval_id,),
            )
        ).fetchone()

    async def tell(text: str) -> None:
        await _tell_clicker(request, org_id, user, payload, text)

    if row is None:  # unknown, or another org's (row level security)
        await tell("This approval request no longer exists.")
        return
    status, message_ref, summary, workflow_id, expired, task_id = row
    if status != "pending" or expired:
        state = "expired" if expired and status == "pending" else status
        await tell(f"This request was already {state}. Decisions are final.")
        return
    if member is None:
        await tell("You can't decide this request.")
        return
    try:
        async with tenant_tx(pool, org_id) as conn:
            await conn.execute(
                "update app.approvals set status = %s, decided_by = %s where id = %s",
                (decision, member[0], approval_id),
            )
            await conn.execute(
                "insert into app.audit_log (org_id, actor_type, actor_id, task_id, action, target, decision) "
                "values (%s, 'user', %s, %s, %s, %s, %s)",
                (org_id, member[0], task_id, f"approval.{decision}", summary[:500],
                 "allowed" if decision == "approved" else "denied"),
            )  # fmt: skip
    except psycopg.errors.InsufficientPrivilege:
        await tell("Only the person who asked or an admin can decide this.")
        return
    except psycopg.errors.CheckViolation:
        await tell("This request was already decided or has expired.")
        return
    try:
        handle = (await temporal(request)).get_workflow_handle(workflow_id)
        await handle.signal("approval_decided", args=[str(approval_id), decision])
    except Exception:  # the task re-reads the decision from the database while it waits
        log.warning("could not signal the task", extra={"workflow_id": workflow_id}, exc_info=True)
    team, token = await service.bot_token(pool, vault, org_id)
    outcome = f"✅ Approved by <@{user}>" if decision == "approved" else f"❌ Rejected by <@{user}>"
    channel, _, ts = str(message_ref or "").rpartition(":")
    if channel and ts:
        try:
            await slack.call(
                "chat.update", token, team, channel=channel, ts=ts, text=outcome,
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": summary[:2900]}},
                        {"type": "context", "elements": [{"type": "mrkdwn", "text": outcome}]}],
            )  # fmt: skip
        except SlackError as exc:
            log.warning("could not update the approval message: %s", exc.error)


async def _cancel_task(
    request: Request, org_id: UUID, workspace_id: UUID, user: str, value: str, payload: dict[str, Any]
) -> None:
    pool, _, _ = _required(request)
    try:
        task_id = UUID(value)
    except ValueError:
        return
    async with tenant_tx(pool, org_id) as conn:
        member = await _member(conn, workspace_id, user)
        task = await (
            await conn.execute(
                "select requested_by, workflow_id, status from app.tasks where id = %s", (task_id,)
            )
        ).fetchone()

    async def tell(text: str) -> None:
        await _tell_clicker(request, org_id, user, payload, text)

    if task is None:
        await tell("This task no longer exists.")
        return
    requested_by, workflow_id, status = task
    if (
        member is None
        or member[2] == "removed"
        or not (member[0] == requested_by or (member[1] == "admin" and member[2] == "active"))
    ):
        await tell("Only the person who asked or an admin can cancel this.")
        return
    if status in ("completed", "failed", "cancelled"):
        await tell(f"This task is already {status}.")
        return
    async with tenant_tx(pool, org_id) as conn:
        await conn.execute(
            "insert into app.audit_log (org_id, actor_type, actor_id, task_id, action) "
            "values (%s, 'user', %s, %s, 'task.cancelled')",
            (org_id, member[0], task_id),
        )
    await (await temporal(request)).get_workflow_handle(workflow_id).cancel()


# ------------------------------------------------------------------ OAuth install


@router.get("/slack/oauth/callback", include_in_schema=False)
async def oauth_callback(request: Request) -> Response:
    settings = get_settings()

    def back(**params: str) -> RedirectResponse:
        return RedirectResponse(f"{settings.console_url}/slack?{urlencode(params)}", status_code=302)

    if request.query_params.get("error"):
        return back(error=request.query_params["error"])  # the admin cancelled on Slack
    code, state = request.query_params.get("code"), request.query_params.get("state")
    res = resources(request)
    if (
        not code
        or not state
        or settings.slack_state_secret is None
        or not settings.slack_client_id
        or settings.slack_client_secret is None
    ):
        return back(error="bad_request")
    try:
        claims = service.read_state(settings.slack_state_secret.get_secret_value(), state)
    except (service.InstallError, ValueError, KeyError) as exc:
        return back(error=getattr(exc, "code", "bad_state"))
    if res.kv is not None and not await kv.claim(
        res.kv, kv.global_key("slack-oauth-state", claims["nonce"]), service.STATE_TTL
    ):
        return back(error="state_reused")
    assert res.slack is not None
    assert res.db_pool is not None
    assert res.vault is not None
    try:
        oauth = await res.slack.call(
            "oauth.v2.access", code=code, redirect_uri=f"{settings.api_public_url}/slack/oauth/callback",
            auth=(settings.slack_client_id, settings.slack_client_secret.get_secret_value()),
        )  # fmt: skip
    except SlackError as exc:
        log.warning("slack code exchange failed: %s", exc.error)
        return back(error="exchange_failed")
    org_id, membership_id = UUID(claims["org_id"]), UUID(claims["membership_id"])
    try:
        async with tenant_tx(res.db_pool, org_id) as conn:
            admin = await (
                await conn.execute(
                    "select 1 from app.org_memberships where id = %s and privilege = 'admin' and status "
                    "= 'active'",
                    (membership_id,),
                )
            ).fetchone()
            if admin is None:
                raise service.InstallError("not_admin", "Only admins can connect Slack.")
            installed = await service.install(conn, org_id, membership_id, oauth)
    except service.InstallError as exc:
        return back(error=exc.code)
    try:
        await res.vault.store(org_id, installed.connection_id, str(oauth["access_token"]))
    except Exception:
        # Undo the install so the console does not show a connection without a token, and revoke
        # the token at Slack so it does not outlive us. The admin can simply connect again.
        log.exception("could not store the slack token; undoing the install")
        try:
            await res.slack.call("auth.revoke", str(oauth["access_token"]))
        except SlackError:
            log.warning("could not revoke the unstored slack token", exc_info=True)
        async with tenant_tx(res.db_pool, org_id) as conn:
            await service.uninstall(
                conn, None, org_id, installed.team_id, "the bot token could not be stored"
            )
        return back(error="storage_failed")
    try:
        await start(
            request,
            "SyncWorkspaceWorkflow",
            SyncRequest(str(org_id), str(installed.workspace_id), installed.team_id),
            f"slack-sync-install-{installed.connection_id}",
        )
    except Exception:
        log.exception("could not start the first channel sync")  # the console's Sync button recovers
    return back(installed=installed.team_name)

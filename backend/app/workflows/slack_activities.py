"""Slack activities. Each runs its own tenant transactions as ``worker_service`` and reads the bot
token through the vault; nothing here trusts the model or message text."""

import logging
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.db.tenant import tenant_tx
from app.slack import service
from app.slack.adapter import ChannelAdapter, DirectMessage, Ephemeral, Intent, Reply
from app.slack.client import SlackClient, SlackError
from app.vault import Vault
from app.workflows.slack_types import (
    ChannelCheck,
    HomeRequest,
    Requester,
    SlackRequest,
    SyncRequest,
    SyncResult,
)

log = logging.getLogger(__name__)

# Slack errors that retrying cannot fix: fail the activity at once instead of retrying.
PERMANENT_SLACK_ERRORS = {
    "not_in_channel", "channel_not_found", "is_archived", "user_not_in_channel", "user_not_found",
    "invalid_auth", "account_inactive", "token_revoked", "missing_scope", "not_allowed_token_type",
    "invalid_blocks", "msg_too_long", "no_text", "cant_update_message", "message_not_found",
}  # fmt: skip


def permanent(exc: SlackError) -> ApplicationError:
    return ApplicationError(
        f"slack {exc.method}: {exc.error}", type="SlackPermanentError", non_retryable=True
    )


@dataclass
class SlackDeps:
    pool: AsyncConnectionPool  # worker_service (app_user)
    vault: Vault
    slack: SlackClient
    kv: Any
    console_url: str


class SlackActivities:
    def __init__(self, deps: SlackDeps) -> None:
        self.d = deps
        self.adapter = ChannelAdapter(deps.slack, deps.kv)

    async def _token(self, org_id: str) -> tuple[str, str]:
        try:
            return await service.bot_token(self.d.pool, self.d.vault, org_id)
        except service.SlackNotConnectedError as exc:
            raise ApplicationError(str(exc), type="SlackNotConnected", non_retryable=True) from exc

    async def _tell_privately(self, team: str, token: str, req: SlackRequest, text: str) -> None:
        """An ephemeral message where they wrote, or a DM when that is not possible (for example
        the bot was removed from the channel). Never fails the workflow over a notice."""
        attempts: list[Intent] = (
            [Reply(req.channel, text)]
            if req.channel_type == "im"
            else [Ephemeral(req.channel, req.user, text, req.thread_ts), DirectMessage(req.user, text)]
        )
        for intent in attempts:
            try:
                await self.adapter.send(team, token, intent)
                return
            except SlackError as exc:
                if exc.error not in PERMANENT_SLACK_ERRORS:
                    raise
                log.info(
                    "private notice not delivered, trying the next way", extra={"slack_error": exc.error}
                )
        log.warning("could not tell the requester privately", extra={"user": req.user})

    async def _membership_for(self, conn: AsyncConnection, workspace_id: str, user: str) -> Requester | None:
        row = await (
            await conn.execute(
                "select m.id, u.email, m.status from app.platform_identities pi "
                "join app.org_memberships m on m.id = pi.membership_id join app.users u on u.id = m.user_id "
                "where pi.workspace_id = %s and pi.external_user_id = %s",
                (workspace_id, user),
            )
        ).fetchone()
        if row is None:
            return None
        if row[2] not in ("active", "invited"):
            return Requester(None, row[1], row[2])
        return Requester(str(row[0]), row[1], row[2])

    async def _identify(self, org_id: str, workspace_id: str, user: str) -> Requester:
        """Platform identity first; otherwise match the Slack email to an active or invited member
        (spec 4.1.2: invited users work before console signup) and record the identity."""
        async with tenant_tx(self.d.pool, org_id) as conn:
            known = await self._membership_for(conn, workspace_id, user)
        if known is not None:
            return known
        team, token = await self._token(org_id)
        info = (await self.d.slack.call("users.info", token, team, user=user))["user"]
        if info.get("is_bot") or info.get("deleted"):
            return Requester(None)
        email = (info.get("profile") or {}).get("email")
        if not email:
            return Requester(None)
        async with tenant_tx(self.d.pool, org_id) as conn:
            row = await (
                await conn.execute(
                    "select m.id, m.status from app.org_memberships m join app.users u on u.id = m.user_id "
                    "where lower(u.email) = lower(%s) and m.status in ('active', 'invited')",
                    (email,),
                )
            ).fetchone()
            if row is None:
                return Requester(None, email)
            await conn.execute(
                "insert into app.platform_identities (org_id, membership_id, workspace_id, "
                "external_user_id, matched_email) "
                "values (%s, %s, %s, %s, %s) on conflict do nothing",
                (org_id, row[0], workspace_id, user, email),
            )
            linked = await self._membership_for(conn, workspace_id, user)
        # The membership may already be linked to a different Slack user (an email reused): refuse.
        return linked if linked and linked.membership_id == str(row[0]) else Requester(None, email)

    @activity.defn(name="slack_identify_requester")
    async def identify_requester(self, req: SlackRequest) -> Requester:
        return await self._identify(req.org_id, req.workspace_id, req.user)

    @activity.defn(name="slack_request_access")
    async def request_access(self, req: SlackRequest, requester: Requester) -> bool:
        team, token = await self._token(req.org_id)
        async with tenant_tx(self.d.pool, req.org_id) as conn:
            row = await (
                await conn.execute(
                    "insert into app.access_requests (org_id, workspace_id, external_user_id, email) "
                    "values (%s, %s, %s, %s) "
                    "on conflict (workspace_id, external_user_id) where status = 'pending' do nothing "
                    "returning id",
                    (req.org_id, req.workspace_id, req.user, requester.email),
                )
            ).fetchone()
            org = await (await conn.execute("select name from app.orgs")).fetchone()
            admins = [
                r[0]
                for r in await (
                    await conn.execute(
                        "select pi.external_user_id from app.org_memberships m "
                        "join app.platform_identities pi on pi.membership_id = m.id and pi.workspace_id = %s "
                        "where m.privilege = 'admin' and m.status = 'active'",
                        (req.workspace_id,),
                    )
                ).fetchall()
            ]
        org_name = org[0] if org else "this organization"
        text = f"You don't have access to the agent in {org_name} yet. I've asked the admins to invite you."
        await self._tell_privately(team, token, req, text)
        if row is not None:  # a new request: tell the admins once
            who = f"<@{req.user}>" + (f" ({requester.email})" if requester.email else "")
            for admin in admins:
                await self.adapter.send(
                    team,
                    token,
                    DirectMessage(
                        admin,
                        f"{who} asked to use the agent in {org_name}.",
                        actions=[
                            ("access_request_invite", "Invite", str(row[0])),
                            ("access_request_dismiss", "Dismiss", str(row[0])),
                        ],
                    ),
                )
        return row is not None

    @activity.defn(name="slack_check_channel")
    async def check_channel(self, req: SlackRequest, requester: Requester) -> ChannelCheck:
        if req.channel_type == "im":
            return ChannelCheck(True, "direct_message")
        if req.channel_type == "mpim":
            return ChannelCheck(False, "group_dm")
        assert requester.membership_id is not None
        async with tenant_tx(self.d.pool, req.org_id) as conn:
            channel_id = await service.channel_id_for(conn, req.workspace_id, req.channel)
            if channel_id is None:
                channel_id = await service.upsert_channel(
                    conn,
                    req.org_id,
                    req.workspace_id,
                    {"id": req.channel, "is_member": True, "is_private": req.channel_type == "group"},
                )
            # They just posted there, so they are a member: keep the membership copy current.
            await conn.execute(
                "insert into app.channel_members (org_id, channel_id, external_user_id) values (%s, %s, "
                "%s) on conflict do nothing",
                (req.org_id, channel_id, req.user),
            )
            row = await (
                await conn.execute(
                    "select is_allowlisted, archived, app.can_read_channel(%s, id) from app.channels "
                    "where id = %s",
                    (requester.membership_id, channel_id),
                )
            ).fetchone()
        if self.d.kv is not None:
            await self.d.kv.sadd(service.members_key(req.org_id, channel_id), req.user)
        assert row is not None
        allowlisted, archived, can_read = row
        if not allowlisted or archived:
            return ChannelCheck(False, "channel_not_allowlisted", str(channel_id))
        return ChannelCheck(
            bool(can_read), "allowed" if can_read else "not_a_channel_member", str(channel_id)
        )

    @activity.defn(name="slack_explain_refusal")
    async def explain_refusal(self, req: SlackRequest, check: ChannelCheck) -> None:
        team, token = await self._token(req.org_id)
        messages = {
            "channel_not_allowlisted": (
                "I'm not enabled in this channel. "
                f"An admin can turn it on in the console: {self.d.console_url}/slack"
            ),
            "group_dm": (
                "I don't work in group DMs yet. Mention me in an enabled channel or message me directly."
            ),
            "not_a_channel_member": "I can only work in channels you are a member of.",
        }
        await self._tell_privately(team, token, req, messages.get(check.reason, "I can't help here."))

    @activity.defn(name="slack_acknowledge")
    async def acknowledge(self, req: SlackRequest, requester: Requester) -> str | None:
        """Phase 6 stub reply. Idempotent: a retried activity does not post twice."""
        key = f"ap:t:{req.org_id}:sent:{req.event_id}:ack"
        if self.d.kv is not None and not await self.d.kv.set(key, "pending", nx=True, ex=24 * 3600):
            return None
        team, token = await self._token(req.org_id)
        try:
            (sent,) = await self.adapter.send(
                team,
                token,
                Reply(
                    req.channel,
                    f"Received, <@{req.user}>. I'll reply in this thread.",
                    req.thread_ts or req.ts,
                ),
            )
        except Exception as exc:
            if self.d.kv is not None:
                await self.d.kv.delete(key)
            if isinstance(exc, SlackError) and exc.error in PERMANENT_SLACK_ERRORS:
                raise permanent(exc) from exc
            raise
        if self.d.kv is not None:
            await self.d.kv.set(key, str(sent.get("ts")), ex=24 * 3600)
        return str(sent.get("ts"))

    @activity.defn(name="slack_sync_workspace")
    async def sync_workspace(self, req: SyncRequest) -> SyncResult:
        team, token = await self._token(req.org_id)
        listed = [
            c
            async for c in self.d.slack.paginate(
                "conversations.list",
                "channels",
                token,
                team,
                types="public_channel,private_channel",
                exclude_archived=False,
            )
        ]
        seen = {c["id"] for c in listed}
        async with tenant_tx(self.d.pool, req.org_id) as conn:
            for channel in listed:
                await service.upsert_channel(conn, req.org_id, req.workspace_id, channel)
            gone = await (
                await conn.execute(
                    "update app.channels set archived = true where workspace_id = %s and not archived "
                    "and not (external_id = any(%s)) returning id",
                    (req.workspace_id, list(seen)),
                )
            ).fetchall()
            targets = await (
                await conn.execute(
                    "select id, external_id from app.channels where workspace_id = %s and is_allowlisted "
                    "and not archived and bot_is_member "
                    "and (cardinality(%s::uuid[]) = 0 or id = any(%s::uuid[]))",
                    (req.workspace_id, req.channel_ids, req.channel_ids),
                )
            ).fetchall()
        for channel_id, external_id in targets:
            members = [
                m
                async for m in self.d.slack.paginate(
                    "conversations.members", "members", token, team, channel=external_id
                )
            ]
            async with tenant_tx(self.d.pool, req.org_id) as conn:
                await service.set_members(conn, self.d.kv, req.org_id, channel_id, members)
        log.info(
            "slack sync done",
            extra={"channels": len(listed), "member_lists": len(targets), "archived": len(gone)},
        )
        return SyncResult(len(listed), len(targets), len(gone))

    @activity.defn(name="slack_publish_home")
    async def publish_home(self, req: HomeRequest) -> str:
        requester = await self._identify(req.org_id, req.workspace_id, req.user)
        team, token = await self._token(req.org_id)
        if requester.membership_id is None:
            view = home_view_no_access()
        else:
            async with tenant_tx(self.d.pool, req.org_id) as conn:
                row = await (
                    await conn.execute(
                        """
                        select o.name,
                               coalesce((select mode from app.user_preferences
                                          where membership_id = %(m)s), 'ask'),
                               app.effective_mode(%(m)s),
                               coalesce((select max_mode from app.autonomy_policies
                                          where scope_type = 'org'), 'ask'),
                               (select string_agg(
                                         r.title || case when mr.is_primary then ' (primary)' else '' end,
                                         ', '
                                                  order by mr.is_primary desc, r.title)
                                  from app.membership_roles mr
                                  join app.custom_roles r on r.id = mr.role_id and r.status = 'active'
                                 where mr.membership_id = %(m)s),
                               m.status
                          from app.org_memberships m join app.orgs o on o.id = m.org_id
                         where m.id = %(m)s
                        """,
                        {"m": requester.membership_id},
                    )
                ).fetchone()
            assert row is not None
            view = home_view(
                org=row[0],
                email=requester.email or "",
                mode=row[1],
                effective=row[2],
                ceiling=row[3],
                roles=row[4],
                invited=row[5] == "invited",
                console_url=self.d.console_url,
            )
        await self.d.slack.call("views.publish", token, team, user_id=req.user, view=view)
        return "published"


def home_view(
    org: str,
    email: str,
    mode: str,
    effective: str,
    ceiling: str,
    roles: str | None,
    invited: bool,
    console_url: str,
) -> dict[str, Any]:
    options = [
        {"text": {"type": "plain_text", "text": "Ask before changing anything"}, "value": "ask"},
        {"text": {"type": "plain_text", "text": "Auto: act within my access"}, "value": "auto"},
    ]
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Agent"}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{org}*\nSigned in as {email}" + (" (invitation pending)" if invited else ""),
            },
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"Roles: {roles or 'none'}"}]},
        {"type": "divider"},
        {
            "type": "section",
            "block_id": "home_mode",
            "text": {"type": "mrkdwn", "text": "*How should I act?*"},
            "accessory": {
                "type": "static_select",
                "action_id": "home_mode",
                "options": options,
                "initial_option": options[0] if mode == "ask" else options[1],
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Your admins allow up to *{ceiling}*; you are working in *{effective}* mode. "
                        "Irreversible actions always need approval."
                    ),
                }
            ],
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Linked accounts and schedules: <{console_url}|open the console>.",
                }
            ],
        },
    ]
    return {"type": "home", "blocks": blocks}


def home_view_no_access() -> dict[str, Any]:
    return {
        "type": "home",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "Agent"}},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "You don't have access to the agent yet."},
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "home_request_access",
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Request access"},
                        "value": "request",
                    }
                ],
            },
        ],
    }

"""Slack domain logic shared by ingress, the install flow, workers and the console."""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app import kv as kvlib
from app.db.tenant import tenant_tx
from app.vault import Vault

log = logging.getLogger(__name__)

BOT_SCOPES = [
    "app_mentions:read",
    "channels:read",
    "groups:read",
    "channels:history",
    "groups:history",
    "im:history",
    "chat:write",
    "users:read",
    "users:read.email",
]


class SlackNotConnectedError(LookupError):
    pass


class InstallError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ OAuth state


STATE_TTL = 600


def make_state(
    secret: str, org_id: UUID, membership_id: UUID, nonce: str | None = None, now: float | None = None
) -> str:
    payload = {
        "o": str(org_id),
        "m": str(membership_id),
        "n": nonce or secrets.token_urlsafe(16),
        "e": int((now or time.time()) + STATE_TTL),
    }
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def read_state(secret: str, state: str, now: float | None = None) -> dict[str, str]:
    """Verify signature and expiry. Single use is enforced by the caller (Key Value nonce)."""
    body, _, signature = state.partition(".")
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not body or not hmac.compare_digest(signature, expected):
        raise InstallError("bad_state", "The install link is not valid. Start again from the console.")
    payload: dict[str, Any] = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    if int(payload["e"]) < (now or time.time()):
        raise InstallError("expired_state", "The install link expired. Start again from the console.")
    return {"org_id": payload["o"], "membership_id": payload["m"], "nonce": payload["n"]}


# ------------------------------------------------------------------ tokens


async def bot_connection(conn: AsyncConnection) -> tuple[UUID, str] | None:
    """The org's active Slack connection: (connection id, team id). Needs a tenant transaction."""
    row = await (
        await conn.execute(
            "select c.id, w.external_id from app.connections c "
            "join app.workspaces w on w.org_id = c.org_id and w.status = 'active' and w.external_id = "
            "c.external_account_id "
            "where c.integration_key = 'slack' and c.scope = 'org' and c.status = 'active'"
        )
    ).fetchone()
    return (row[0], row[1]) if row else None


async def bot_token(pool: AsyncConnectionPool, vault: Vault, org_id: UUID | str) -> tuple[str, str]:
    """(team id, bot token) for an org."""
    async with tenant_tx(pool, org_id) as conn:
        found = await bot_connection(conn)
    if found is None:
        raise SlackNotConnectedError(f"org {org_id} has no active Slack connection")
    connection_id, team_id = found
    return team_id, await vault.read(org_id, connection_id)


# ------------------------------------------------------------------ install and uninstall


@dataclass(frozen=True)
class Installed:
    workspace_id: UUID
    connection_id: UUID
    team_id: str
    team_name: str


async def install(
    conn: AsyncConnection, org_id: UUID, membership_id: UUID, oauth: dict[str, Any]
) -> Installed:
    """Record an OAuth v2 install. ``conn`` is a tenant transaction for ``org_id`` (app_user).

    The bot token is NOT stored here: the vault writes in its own transaction (as vault_service),
    which can only see the connection row once this transaction has committed. The caller
    commits, then calls ``vault.store`` (and revokes the connection if that fails)."""
    if oauth.get("token_type") != "bot" or not str(oauth.get("access_token", "")).startswith("xoxb-"):
        raise InstallError("not_a_bot_token", "Slack did not return a bot token.")
    team = oauth.get("team") or {}
    team_id, team_name = str(team.get("id", "")), str(team.get("name", ""))
    if not team_id:
        raise InstallError("no_team", "Slack did not say which workspace was installed.")
    current = await (
        await conn.execute("select external_id from app.workspaces where status = 'active'")
    ).fetchone()
    if current and current[0] != team_id:
        raise InstallError(
            "other_workspace", "This organization is already connected to another Slack workspace."
        )
    resolved = await (
        await conn.execute("select org_id from app.resolve_workspace('slack', %s)", (team_id,))
    ).fetchone()
    if resolved and resolved[0] != org_id:
        raise InstallError(
            "workspace_taken", "That Slack workspace is already connected to another organization."
        )

    workspace = await (
        await conn.execute(
            """
            insert into app.workspaces
              (org_id, platform, external_id, enterprise_id, name, bot_user_id, installed_by)
            values (%s, 'slack', %s, %s, %s, %s, %s)
            on conflict (platform, external_id) where status = 'active' do update
              set name = excluded.name, bot_user_id = excluded.bot_user_id,
                  installed_by = excluded.installed_by,
                  installed_at = now()
            returning id
            """,
            (
                org_id,
                team_id,
                (oauth.get("enterprise") or {}).get("id"),
                team_name,
                oauth.get("bot_user_id"),
                membership_id,
            ),
        )
    ).fetchone()
    assert workspace is not None
    await conn.execute(
        """
        insert into app.org_integrations (org_id, integration_key, kind, enabled)
        values (%s, 'slack', 'native', true)
        on conflict (org_id, integration_key) do update set enabled = true
        """,
        (org_id,),
    )
    await conn.execute(
        "update app.connections set status = 'revoked' where integration_key = 'slack' and scope = 'org' "
        "and status = 'active'"
    )
    connection = await (
        await conn.execute(
            """
            insert into app.connections (org_id, integration_key, scope, external_account_id, granted_scopes)
            values (%s, 'slack', 'org', %s, %s) returning id
            """,
            (org_id, team_id, str(oauth.get("scope", "")).split(",")),
        )
    ).fetchone()
    assert connection is not None
    await conn.execute(
        "insert into app.audit_log (org_id, actor_type, actor_id, action, target, metadata) values (%s, "
        "'user', %s, 'slack.installed', %s, %s)",
        (org_id, membership_id, team_id, Jsonb({"team_name": team_name, "scopes": oauth.get("scope", "")})),
    )
    return Installed(workspace[0], connection[0], team_id, team_name)


async def uninstall(
    conn: AsyncConnection,
    vault: Vault | None,
    org_id: UUID,
    team_id: str,
    reason: str,
    actor: UUID | None = None,
) -> bool:
    """Mark the workspace uninstalled, revoke its connection and delete the token (``vault`` None:
    no token was stored)."""
    row = await (
        await conn.execute(
            "update app.workspaces set status = 'uninstalled' where external_id = %s and status = "
            "'active' returning id",
            (team_id,),
        )
    ).fetchone()
    connections = await (
        await conn.execute(
            "update app.connections set status = 'revoked' where integration_key = 'slack' and "
            "external_account_id = %s and status = 'active' returning id",
            (team_id,),
        )
    ).fetchall()
    for (connection_id,) in connections:
        if vault is not None:
            await vault.delete(org_id, connection_id)
    if row:
        await conn.execute(
            "insert into app.audit_log (org_id, actor_type, actor_id, action, target, reason) values "
            "(%s, %s, %s, 'slack.uninstalled', %s, %s)",
            (org_id, "user" if actor else "system", actor, team_id, reason),
        )
    return row is not None


# ------------------------------------------------------------------ channels and members


def members_key(org_id: UUID | str, channel_id: UUID | str) -> str:
    return kvlib.tenant_key(UUID(str(org_id)), "chm", str(channel_id))


async def upsert_channel(
    conn: AsyncConnection, org_id: UUID | str, workspace_id: UUID | str, channel: dict[str, Any]
) -> UUID:
    row = await (
        await conn.execute(
            """
            insert into app.channels
              (org_id, workspace_id, external_id, name, is_private, bot_is_member, archived)
            values (%s, %s, %s, %s, %s, %s, %s)
            on conflict (workspace_id, external_id) do update
              set name = excluded.name, is_private = excluded.is_private,
                  bot_is_member = coalesce(excluded.bot_is_member, app.channels.bot_is_member),
                  archived = excluded.archived
            returning id
            """,
            (
                org_id,
                workspace_id,
                channel["id"],
                channel.get("name"),
                bool(channel.get("is_private") or channel.get("is_group")),
                bool(channel.get("is_member", False)),
                bool(channel.get("is_archived", False)),
            ),
        )
    ).fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


async def set_members(
    conn: AsyncConnection, kv: Any, org_id: UUID | str, channel_id: UUID, members: list[str]
) -> None:
    await conn.execute(
        "delete from app.channel_members where channel_id = %s and not (external_user_id = any(%s))",
        (channel_id, members),
    )
    for user in members:
        await conn.execute(
            "insert into app.channel_members (org_id, channel_id, external_user_id) values (%s, %s, %s) "
            "on conflict (channel_id, external_user_id) do update set synced_at = now()",
            (org_id, channel_id, user),
        )
    if kv is not None:
        key = members_key(org_id, channel_id)
        await kv.delete(key)
        if members:
            await kv.sadd(key, *members)
        await kv.expire(key, 24 * 3600)


async def channel_id_for(
    conn: AsyncConnection, workspace_id: UUID | str, external_channel: str
) -> UUID | None:
    row = await (
        await conn.execute(
            "select id from app.channels where workspace_id = %s and external_id = %s",
            (workspace_id, external_channel),
        )
    ).fetchone()
    return row[0] if row else None


async def apply_channel_event(
    conn: AsyncConnection,
    kv: Any,
    org_id: UUID,
    workspace_id: UUID,
    bot_user_id: str | None,
    event: dict[str, Any],
) -> str:
    """Channel lifecycle and membership events (inline in ingress: small idempotent writes)."""
    kind = event.get("type", "")
    if kind in ("member_joined_channel", "member_left_channel"):
        channel_ext, user = event.get("channel"), event.get("user")
        if not channel_ext or not user:
            return "ignored"
        channel_id = await channel_id_for(conn, workspace_id, channel_ext)
        if channel_id is None:
            channel_id = await upsert_channel(
                conn,
                org_id,
                workspace_id,
                {"id": channel_ext, "is_private": event.get("channel_type") == "G"},
            )
        if user == bot_user_id:
            await conn.execute(
                "update app.channels set bot_is_member = %s where id = %s",
                (kind == "member_joined_channel", channel_id),
            )
            return "bot_membership"
        if kind == "member_joined_channel":
            await conn.execute(
                "insert into app.channel_members (org_id, channel_id, external_user_id) values (%s, %s, "
                "%s) on conflict do nothing",
                (org_id, channel_id, user),
            )
            if kv is not None:
                await kv.sadd(members_key(org_id, channel_id), user)
        else:
            await conn.execute(
                "delete from app.channel_members where channel_id = %s and external_user_id = %s",
                (channel_id, user),
            )
            if kv is not None:
                await kv.srem(members_key(org_id, channel_id), user)
        return "membership"
    channel = event.get("channel")
    if kind in ("channel_created", "group_created") and isinstance(channel, dict):
        await upsert_channel(conn, org_id, workspace_id, {**channel, "is_private": kind == "group_created"})
        return "channel"
    if kind in ("channel_rename", "group_rename") and isinstance(channel, dict):
        await conn.execute(
            "update app.channels set name = %s where workspace_id = %s and external_id = %s",
            (channel.get("name"), workspace_id, channel.get("id")),
        )
        return "channel"
    if kind in ("channel_archive", "group_archive", "channel_deleted", "group_deleted"):
        # Archiving pauses schedules delivering there (database trigger).
        await conn.execute(
            "update app.channels set archived = true where workspace_id = %s and external_id = %s",
            (workspace_id, channel),
        )
        return "channel"
    if kind in ("channel_unarchive", "group_unarchive"):
        await conn.execute(
            "update app.channels set archived = false where workspace_id = %s and external_id = %s",
            (workspace_id, channel),
        )
        return "channel"
    return "ignored"

"""Local development data, loaded from ``fixture.json``.

Runs as ``app_owner`` over the direct migration connection (the owner is not subject to RLS, which
works whether or not managed Postgres allows BYPASSRLS), and is idempotent: every row is upserted
by its natural key, so running it twice changes nothing. Fixture orgs are put back to the fixture:
members it does not declare are removed and pending access requests are dismissed. The same
fixture feeds the local Clerk and Slack stand-ins, so people and channels line up.
"""

import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

log = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).with_name("fixture.json")


def load_fixture(path: Path = FIXTURE_PATH) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def _one(cur: psycopg.Cursor[Any], query: str, params: tuple[Any, ...]) -> UUID:
    row = cur.execute(query, params).fetchone()
    assert row is not None
    value: UUID = row[0]
    return value


def local_bot_token(team_id: str) -> str:
    """The bot token the local Slack stand-in issues for a seeded workspace."""
    return f"xoxb-local-{team_id}"


def seed(database_url: str, fixture: dict[str, Any] | None = None, envelope: Any = None) -> dict[str, UUID]:
    """Load the fixture. Returns org key -> org id. With an envelope, the seeded workspaces also
    get their Slack connection and (encrypted) bot token, so they work without the install flow."""
    data = fixture or load_fixture()
    org_ids: dict[str, UUID] = {}
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("set local role app_owner")

        user_ids: dict[str, UUID] = {}
        for user in data["users"]:
            user_ids[user["email"]] = _one(
                cur,
                """
                insert into app.users (email, name, clerk_user_id) values (%s, %s, %s)
                on conflict ((lower(email))) do update
                  set name = excluded.name, clerk_user_id = excluded.clerk_user_id
                returning id
                """,
                (user["email"], user["name"], user["clerk_user_id"]),
            )

        for org in data["orgs"]:
            org_id = _one(
                cur,
                """
                insert into app.orgs (clerk_org_id, name) values (%s, %s)
                on conflict (clerk_org_id) do update set name = excluded.name
                returning id
                """,
                (org["clerk_org_id"], org["name"]),
            )
            org_ids[org["key"]] = org_id
            _seed_org(cur, org_id, org, user_ids)
            if envelope is not None:
                _seed_slack_connection(cur, envelope, org_id, org["workspace"]["external_id"])

        conn.commit()
    log.info("seeded %d orgs and %d users", len(org_ids), len(data["users"]))
    return org_ids


def _seed_org(cur: psycopg.Cursor[Any], org_id: UUID, org: dict[str, Any], user_ids: dict[str, UUID]) -> None:
    s = org["settings"]
    cur.execute(
        """
        update app.org_settings
           set allowed_model_tiers = %s::app.model_tier[], default_model_tier = %s,
               user_daily_token_limit = %s, org_monthly_token_limit = %s,
               max_concurrent_tasks = %s, max_schedules = %s, retention_days = %s
         where org_id = %s
        """,
        (
            s["allowed_model_tiers"],
            s["default_model_tier"],
            s["user_daily_token_limit"],
            s["org_monthly_token_limit"],
            s["max_concurrent_tasks"],
            s["max_schedules"],
            s["retention_days"],
            org_id,
        ),
    )

    role_ids: dict[str, UUID] = {}
    for role in org["roles"]:
        role_ids[role["key"]] = _one(
            cur,
            """
            insert into app.custom_roles (org_id, title, description) values (%s, %s, %s)
            on conflict (org_id, (lower(title))) where status = 'active'
              do update set description = excluded.description
            returning id
            """,
            (org_id, role["title"], role["description"]),
        )

    ws = org["workspace"]
    workspace_id = _one(
        cur,
        """
        insert into app.workspaces (org_id, platform, external_id, name, bot_user_id)
        values (%s, 'slack', %s, %s, %s)
        on conflict (platform, external_id) where status = 'active'
          do update set name = excluded.name, bot_user_id = excluded.bot_user_id
        returning id
        """,
        (org_id, ws["external_id"], ws["name"], ws["bot_user_id"]),
    )

    for member in org["members"]:
        membership_id = _one(
            cur,
            """
            insert into app.org_memberships (org_id, user_id, privilege, status) values (%s, %s, %s, %s)
            on conflict (org_id, user_id) do update
              set privilege = excluded.privilege, status = excluded.status
            returning id
            """,
            (org_id, user_ids[member["email"]], member["privilege"], member["status"]),
        )
        _seed_member_roles(cur, org_id, membership_id, member, role_ids)
        if member.get("mode"):
            cur.execute(
                """
                insert into app.user_preferences (membership_id, org_id, mode) values (%s, %s, %s)
                on conflict (membership_id) do update set mode = excluded.mode
                """,
                (membership_id, org_id, member["mode"]),
            )
        if member["slack_user_id"]:
            cur.execute(
                """
                insert into app.platform_identities
                  (org_id, membership_id, workspace_id, external_user_id, matched_email)
                values (%s, %s, %s, %s, %s)
                on conflict (workspace_id, external_user_id) do update
                  set membership_id = excluded.membership_id, matched_email = excluded.matched_email
                """,
                (org_id, membership_id, workspace_id, member["slack_user_id"], member["email"]),
            )

    # Back to the fixture: people added since (invited from the console or from Slack) are removed,
    # and open access requests are dismissed, so a reseed gives tests a known starting point.
    cur.execute(
        "update app.org_memberships set status = 'removed' "
        "where org_id = %s and status <> 'removed' and not (user_id = any(%s))",
        (org_id, [user_ids[m["email"]] for m in org["members"]]),
    )
    cur.execute(
        "update app.access_requests set status = 'dismissed', resolved_at = now() "
        "where org_id = %s and status = 'pending'",
        (org_id,),
    )
    # Integrations other than Slack start disabled and unconnected (connections, their secrets and
    # operation overrides go with them).
    cur.execute(
        "delete from app.org_integrations where org_id = %s and integration_key <> 'slack'", (org_id,)
    )
    # Token budgets start unspent (repeated test runs on one day would otherwise exhaust them).
    cur.execute("delete from app.usage_daily where org_id = %s", (org_id,))
    cur.execute("delete from app.usage_events where org_id = %s", (org_id,))

    for channel in org["channels"]:
        channel_id = _one(
            cur,
            """
            insert into app.channels
              (org_id, workspace_id, external_id, name, is_private, is_allowlisted, bot_is_member, archived)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (workspace_id, external_id) do update
              set name = excluded.name, is_private = excluded.is_private,
                  is_allowlisted = excluded.is_allowlisted, bot_is_member = excluded.bot_is_member,
                  archived = excluded.archived
            returning id
            """,
            (
                org_id,
                workspace_id,
                channel["external_id"],
                channel["name"],
                channel["is_private"],
                channel["is_allowlisted"],
                channel["bot_is_member"],
                channel["archived"],
            ),
        )
        cur.execute(
            "delete from app.channel_members where channel_id = %s and not (external_user_id = any(%s))",
            (channel_id, channel["members"]),
        )
        for external_user_id in channel["members"]:
            cur.execute(
                """
                insert into app.channel_members (org_id, channel_id, external_user_id) values (%s, %s, %s)
                on conflict (channel_id, external_user_id) do update set synced_at = now()
                """,
                (org_id, channel_id, external_user_id),
            )

    cur.execute(
        """
        insert into app.autonomy_policies (org_id, scope_type, max_mode) values (%s, 'org', %s)
        on conflict (org_id) where scope_type = 'org' do update set max_mode = excluded.max_mode
        """,
        (org_id, org["autonomy_ceiling"]),
    )
    cur.execute(
        "insert into app.audit_log (org_id, actor_type, action, reason) values (%s, 'system', 'seed', %s)",
        (org_id, "local development fixture loaded"),
    )


def _seed_member_roles(
    cur: psycopg.Cursor[Any],
    org_id: UUID,
    membership_id: UUID,
    member: dict[str, Any],
    role_ids: dict[str, UUID],
) -> None:
    wanted = [role_ids[key] for key in member["roles"]]
    # Clear the primary flag first; the "exactly one primary" check is deferred to commit.
    cur.execute(
        "update app.membership_roles set is_primary = false where membership_id = %s", (membership_id,)
    )
    cur.execute(
        "delete from app.membership_roles where membership_id = %s and not (role_id = any(%s))",
        (membership_id, wanted),
    )
    for key in member["roles"]:
        cur.execute(
            """
            insert into app.membership_roles (org_id, membership_id, role_id, is_primary)
            values (%s, %s, %s, %s)
            on conflict (membership_id, role_id) do update set is_primary = excluded.is_primary
            """,
            (org_id, membership_id, role_ids[key], key == member["primary_role"]),
        )


def _seed_slack_connection(cur: psycopg.Cursor[Any], envelope: Any, org_id: UUID, team_id: str) -> None:
    cur.execute(
        "insert into app.org_integrations (org_id, integration_key, kind, enabled) values (%s, 'slack', "
        "'native', true) "
        "on conflict (org_id, integration_key) do update set enabled = true",
        (org_id,),
    )
    existing = cur.execute(
        "select id from app.connections where org_id = %s and integration_key = 'slack' and scope = "
        "'org' and status = 'active'",
        (org_id,),
    ).fetchone()
    if existing:
        connection_id = existing[0]
    else:
        connection_id = _one(
            cur,
            "insert into app.connections (org_id, integration_key, scope, external_account_id, "
            "granted_scopes) "
            "values (%s, 'slack', 'org', %s, %s) returning id",
            (org_id, team_id, ["chat:write", "users:read", "users:read.email", "app_mentions:read"]),
        )
    sealed = envelope.seal(org_id, local_bot_token(team_id).encode(), f"connection_secrets:{connection_id}")
    cur.execute(
        "insert into app.connection_secrets (connection_id, org_id, ciphertext, kms_key_id) values (%s, "
        "%s, %s, %s) "
        "on conflict (connection_id) do update set ciphertext = excluded.ciphertext, kms_key_id = "
        "excluded.kms_key_id",
        (connection_id, org_id, sealed.ciphertext, sealed.kms_key_id),
    )

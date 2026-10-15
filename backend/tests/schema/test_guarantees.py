"""The database guarantees listed in spec section 13.4, one or more tests each."""

import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from tests.world import World, scalar, tx

APP_USER = "app_user"


def membership(conn: psycopg.Connection[Any], org: UUID, email: str) -> UUID:
    return scalar(  # type: ignore[no-any-return]
        conn,
        "select m.id from app.org_memberships m join app.users u on u.id = m.user_id "
        "where m.org_id = %s and u.email = %s",
        (org, email),
    )


# ------------------------------------------------------------------ users, grants, vault, partitions


def test_users_table_shows_only_people_in_the_current_org(world: World) -> None:
    with tx(world.url, APP_USER, world.acme.id) as conn:
        acme = {r[0] for r in conn.execute("select email from app.users")}
    with tx(world.url, APP_USER, world.globex.id) as conn:
        globex = {r[0] for r in conn.execute("select email from app.users")}
    assert "frank@both.test" in acme & globex
    assert "dave@globex.test" not in acme
    assert "alice@acme.test" not in globex


@pytest.mark.parametrize(
    "statement",
    [
        "insert into app.users (email) values ('new@acme.test')",
        "update app.users set name = 'x'",
        "insert into app.orgs (clerk_org_id, name) values ('org_x', 'X')",
        "update app.orgs set plan = 'enterprise'",
        "update app.org_memberships set privilege = 'admin'",
        "insert into app.org_memberships (org_id, user_id) select org_id, user_id from app.org_memberships limit 1",
        "update app.org_settings set org_id = org_id",
        "delete from app.orgs",
    ],
)
def test_app_user_cannot_write_identity_tables(world: World, statement: str) -> None:
    with tx(world.url, APP_USER, world.acme.id) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(statement)


def test_app_user_can_rename_its_org_and_edit_settings(world: World) -> None:
    with tx(world.url, APP_USER, world.acme.id) as conn:
        assert conn.execute("update app.orgs set name = 'Acme 2'").rowcount == 1
        assert (
            conn.execute(
                "update app.org_settings set retention_days = 30, display_settings = '{\"a\": 1}'"
            ).rowcount
            == 1
        )


def test_only_the_vault_reads_secrets_and_only_for_the_current_org(world: World) -> None:
    with tx(world.url, "app_vault", world.acme.id) as conn:
        rows = conn.execute("select org_id, ciphertext from app.connection_secrets").fetchall()
        assert rows == [(world.acme.id, b"secret-of-acme")]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select count(*) from app.tasks")
    with tx(world.url, "app_vault") as conn:
        assert scalar(conn, "select count(*) from app.connection_secrets") == 0
    for role in ("app_user", "app_identity_sync"):
        with tx(world.url, role, world.acme.id) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select count(*) from app.connection_secrets")


def test_identity_sync_cannot_read_work_data(world: World) -> None:
    for table in ("tasks", "artifacts", "channels", "schedules"):
        with tx(world.url, "app_identity_sync") as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(sql.SQL("select count(*) from app.{}").format(sql.Identifier(table)))


def test_partitions_are_reachable_only_through_the_parent(world: World) -> None:
    with tx(world.url) as conn:
        partitions = [
            r[0]
            for r in conn.execute(
                "select c.relname from pg_inherits i join pg_class c on c.oid = i.inhrelid "
                "join pg_class p on p.oid = i.inhparent where p.relname in ('audit_log', 'usage_events')"
            )
        ]
    assert len(partitions) >= 10  # default + 4 months, for both tables
    for name in partitions:
        with (
            tx(world.url, APP_USER, world.acme.id) as conn,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            conn.execute(sql.SQL("select count(*) from app.{}").format(sql.Identifier(name)))


@pytest.mark.parametrize("table", ["audit_log", "usage_events"])
@pytest.mark.parametrize("statement", ["update app.{} set org_id = org_id", "delete from app.{}"])
def test_audit_and_usage_are_append_only(world: World, table: str, statement: str) -> None:
    with tx(world.url, APP_USER, world.acme.id) as conn:
        conn.execute(
            "insert into app.audit_log (org_id, actor_type, action) values (%s, 'agent', 'test')",
            (world.acme.id,),
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(sql.SQL(statement).format(sql.Identifier(table)))


# ------------------------------------------------------------------ playbooks


def test_builtin_playbooks_shared_org_playbooks_private(world: World) -> None:
    with tx(world.url, APP_USER, world.acme.id) as conn:
        ids = {r[0] for r in conn.execute("select id from app.playbooks")}
    assert world.builtin_playbook in ids
    assert world.acme.ids["playbooks"] in ids
    assert world.globex.ids["playbooks"] not in ids


def test_playbook_versions_are_immutable(world: World) -> None:
    with tx(world.url, APP_USER, world.acme.id) as conn:
        assert (
            conn.execute(
                "update app.playbooks set enabled = false where id = %s", (world.acme.ids["playbooks"],)
            ).rowcount
            == 1
        )
        # Built ins cannot be toggled by an org (policy), and nothing but `enabled` can change (grant).
        assert (
            conn.execute(
                "update app.playbooks set enabled = false where id = %s", (world.builtin_playbook,)
            ).rowcount
            == 0
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("update app.playbooks set instructions = 'changed'")
    with tx(world.url, APP_USER, world.acme.id) as conn, pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "insert into app.playbooks (org_id, key, version, title, instructions) values (%s, 'runbook', 1, 't', 'i')",
            (world.acme.id,),
        )
    with tx(world.url, APP_USER, world.acme.id) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(
            "insert into app.playbooks (org_id, key, version, title, instructions) values (null, 'xx', 1, 't', 'i')"
        )
    with tx(world.url) as conn, pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "insert into app.playbooks (org_id, key, version, title, instructions) values (null, 'qa_summary', 1, 't', 'i')"
        )


# ------------------------------------------------------------------ scratch orgs for tests that commit


@pytest.fixture
def scratch_org(world: World) -> Iterator[dict[str, Any]]:
    """A committed org with two admins and one user; deleted afterwards."""
    tag = uuid.uuid4().hex[:8]
    with psycopg.connect(world.url) as conn:
        org = scalar(
            conn,
            "insert into app.orgs (clerk_org_id, name) values (%s, %s) returning id",
            (f"org_{tag}", f"Scratch {tag}"),
        )
        members: dict[str, UUID] = {}
        for name, privilege in (("admin1", "admin"), ("admin2", "admin"), ("user1", "user")):
            user = scalar(
                conn,
                "insert into app.users (email) values (%s) returning id",
                (f"{name}-{tag}@scratch.test",),
            )
            members[name] = scalar(
                conn,
                "insert into app.org_memberships (org_id, user_id, privilege, status) values (%s, %s, %s, 'active') returning id",
                (org, user, privilege),
            )
        roles = [
            scalar(
                conn,
                "insert into app.custom_roles (org_id, title) values (%s, %s) returning id",
                (org, title),
            )
            for title in ("First", "Second", "Third")
        ]
        conn.commit()
    yield {"org": org, "members": members, "roles": roles, "tag": tag}
    with psycopg.connect(world.url) as conn:
        conn.execute("delete from app.orgs where id = %s", (org,))
        conn.execute("delete from app.users where email like %s", (f"%-{tag}@scratch.test",))


# ------------------------------------------------------------------ last admin


def test_last_admin_cannot_be_demoted_removed_or_deleted(world: World) -> None:
    dave = world.globex.admin
    for statement in (
        "update app.org_memberships set privilege = 'user' where id = %s",
        "update app.org_memberships set status = 'removed' where id = %s",
        "delete from app.org_memberships where id = %s",
    ):
        with (
            tx(world.url) as conn,
            pytest.raises(psycopg.errors.CheckViolation, match="at least one active admin"),
        ):
            conn.execute(statement, (dave,))


def test_demoting_one_of_two_admins_works(scratch_org: dict[str, Any], world: World) -> None:
    with tx(world.url) as conn:
        assert (
            conn.execute(
                "update app.org_memberships set privilege = 'user' where id = %s",
                (scratch_org["members"]["admin1"],),
            ).rowcount
            == 1
        )


def test_concurrent_demotions_keep_one_admin(scratch_org: dict[str, Any], world: World) -> None:
    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def demote(name: str) -> None:
        with psycopg.connect(world.url) as conn:
            try:
                barrier.wait(timeout=10)
                conn.execute(
                    "update app.org_memberships set privilege = 'user' where id = %s",
                    (scratch_org["members"][name],),
                )
                conn.commit()
                outcomes[name] = "ok"
            except psycopg.errors.CheckViolation:
                outcomes[name] = "refused"

    threads = [threading.Thread(target=demote, args=(n,)) for n in ("admin1", "admin2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(outcomes.values()) == ["ok", "refused"]
    with tx(world.url) as conn:
        assert (
            scalar(
                conn,
                "select count(*) from app.org_memberships where org_id = %s and privilege = 'admin' and status = 'active'",
                (scratch_org["org"],),
            )
            == 1
        )


# ------------------------------------------------------------------ roles: several, exactly one primary


def _add_role(conn: psycopg.Connection[Any], s: dict[str, Any], index: int, primary: bool) -> None:
    conn.execute(
        "insert into app.membership_roles (org_id, membership_id, role_id, is_primary) values (%s, %s, %s, %s)",
        (s["org"], s["members"]["user1"], s["roles"][index], primary),
    )


def test_roles_without_a_primary_fail_at_commit(scratch_org: dict[str, Any], world: World) -> None:
    with psycopg.connect(world.url) as conn:
        _add_role(conn, scratch_org, 0, False)
        _add_role(conn, scratch_org, 1, False)  # no error yet: the check is deferred
        with pytest.raises(psycopg.errors.CheckViolation, match="no primary role"):
            conn.commit()


def test_two_primaries_are_rejected(scratch_org: dict[str, Any], world: World) -> None:
    with tx(world.url) as conn:
        _add_role(conn, scratch_org, 0, True)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _add_role(conn, scratch_org, 1, True)


def test_switching_primary_in_one_transaction_works(scratch_org: dict[str, Any], world: World) -> None:
    member = scratch_org["members"]["user1"]
    with psycopg.connect(world.url) as conn:
        _add_role(conn, scratch_org, 0, True)
        _add_role(conn, scratch_org, 1, False)
        conn.commit()
        conn.execute("update app.membership_roles set is_primary = false where membership_id = %s", (member,))
        conn.execute(
            "update app.membership_roles set is_primary = true where membership_id = %s and role_id = %s",
            (member, scratch_org["roles"][1]),
        )
        conn.commit()
        assert (
            scalar(
                conn,
                "select role_id from app.membership_roles where membership_id = %s and is_primary",
                (member,),
            )
            == scratch_org["roles"][1]
        )


def test_deleting_the_primary_promotes_the_oldest_remaining(
    scratch_org: dict[str, Any], world: World
) -> None:
    member = scratch_org["members"]["user1"]
    with psycopg.connect(world.url) as conn:
        _add_role(conn, scratch_org, 0, True)
        conn.commit()
        _add_role(conn, scratch_org, 1, False)
        conn.commit()
        _add_role(conn, scratch_org, 2, False)
        conn.commit()
        conn.execute("delete from app.custom_roles where id = %s", (scratch_org["roles"][0],))  # cascades
        conn.commit()
        assert (
            scalar(
                conn,
                "select role_id from app.membership_roles where membership_id = %s and is_primary",
                (member,),
            )
            == scratch_org["roles"][1]
        )


# ------------------------------------------------------------------ effective mode and channel rule


def test_effective_mode(world: World) -> None:
    a = world.acme
    repo = a.ids["repositories"]
    with tx(world.url, APP_USER, a.id) as conn:

        def mode(member: UUID, channel: UUID | None = None, repository: UUID | None = None) -> str:
            return str(scalar(conn, "select app.effective_mode(%s, %s, %s)", (member, channel, repository)))

        quinn = membership(conn, a.id, "quinn@acme.test")
        assert mode(quinn) == "ask"  # no preference row: default ask
        conn.execute(
            "insert into app.user_preferences (membership_id, org_id, mode) values (%s, %s, 'auto')",
            (quinn, a.id),
        )
        assert mode(quinn) == "auto"  # org ceiling auto
        assert mode(quinn, a.channel) == "auto"  # channel ceiling auto
        assert mode(quinn, a.channel, repo) == "ask"  # repo ceiling ask wins over channel
        conn.execute("update app.autonomy_policies set max_mode = 'auto' where repository_id = %s", (repo,))
        conn.execute("update app.autonomy_policies set max_mode = 'ask' where channel_id = %s", (a.channel,))
        assert mode(quinn, a.channel) == "ask"  # channel over org
        assert mode(quinn, a.channel, repo) == "auto"  # repo over channel
        conn.execute("delete from app.autonomy_policies")
        assert mode(quinn) == "ask"  # no ceiling at all: ask
        assert mode(a.user) == "ask"  # bob chose ask


def test_can_read_channel(world: World) -> None:
    a = world.acme
    with tx(world.url) as conn:

        def can(member: UUID) -> bool:
            return bool(scalar(conn, "select app.can_read_channel(%s, %s)", (member, a.channel)))

        assert can(a.user)
        quinn = membership(conn, a.id, "quinn@acme.test")
        assert can(quinn)
        conn.execute("update app.org_memberships set status = 'removed' where id = %s", (quinn,))
        assert not can(quinn)  # removed member
        assert not can(world.globex.user)  # member of another org
        conn.execute("update app.channels set is_allowlisted = false where id = %s", (a.channel,))
        assert not can(a.user)
        conn.execute(
            "update app.channels set is_allowlisted = true, archived = true where id = %s", (a.channel,)
        )
        assert not can(a.user)


def test_invited_member_with_a_slack_identity_can_read(world: World) -> None:
    a = world.acme
    with tx(world.url) as conn:
        carol = membership(conn, a.id, "carol@acme.test")
        conn.execute(
            "insert into app.platform_identities (org_id, membership_id, workspace_id, external_user_id, matched_email) "
            "values (%s, %s, %s, 'U0CAROL', 'carol@acme.test')",
            (a.id, carol, a.workspace),
        )
        general = scalar(conn, "select id from app.channels where org_id = %s and name = 'general'", (a.id,))
        assert scalar(conn, "select app.can_read_channel(%s, %s)", (carol, general)) is True


def test_resolvers_work_without_tenant_context(world: World) -> None:
    with tx(world.url, APP_USER) as conn:
        assert conn.execute("select * from app.resolve_workspace('slack', 'T0ACME')").fetchall() == [
            (world.acme.id, world.acme.workspace)
        ]
        assert conn.execute("select * from app.resolve_workspace('slack', 'T0NOPE')").fetchall() == []
        assert conn.execute(
            "select * from app.resolve_task_links('pull_request', 'acme/api#1')"
        ).fetchall() == [(world.acme.id, world.acme.ids["tasks"], world.acme.ids["sessions"])]


def test_resolve_workspace_ignores_uninstalled_workspaces_and_suspended_orgs(world: World) -> None:
    with tx(world.url) as conn:
        conn.execute(
            "update app.workspaces set status = 'uninstalled' where id = %s", (world.acme.workspace,)
        )
        assert conn.execute("select * from app.resolve_workspace('slack', 'T0ACME')").fetchall() == []
    with tx(world.url) as conn:
        conn.execute("update app.orgs set status = 'suspended' where id = %s", (world.globex.id,))
        assert conn.execute("select * from app.resolve_workspace('slack', 'T0GLOBEX')").fetchall() == []


def test_resolver_functions_are_not_callable_by_public(world: World) -> None:
    with tx(world.url) as conn:
        conn.execute("create role tmp_nobody nologin")
        conn.execute("set local role tmp_nobody")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select * from app.resolve_workspace('slack', 'T0ACME')")


# ------------------------------------------------------------------ tasks


def test_one_running_task_per_session(world: World) -> None:
    a = world.acme
    insert = (
        "insert into app.tasks (org_id, session_id, requested_by, trigger, model_tier, status) "
        "values (%s, %s, %s, 'mention', 'fast', 'running')"
    )
    with tx(world.url, APP_USER, a.id) as conn:
        conn.execute(insert, (a.id, a.ids["sessions"], a.user))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(insert, (a.id, a.ids["sessions"], a.user))


@pytest.mark.parametrize(
    "statement",
    [
        "insert into app.tasks (org_id, trigger, model_tier, status) values (%s, 'mention', 'fast', 'completed')",
        "insert into app.tasks (org_id, schedule_id, trigger, model_tier) "
        "select %s, id, 'mention', 'fast' from app.schedules limit 1",
    ],
    ids=["terminal_needs_finish_time", "schedule_needs_schedule_trigger"],
)
def test_task_checks(world: World, statement: str) -> None:
    with tx(world.url, APP_USER, world.acme.id) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(statement, (world.acme.id,))


# ------------------------------------------------------------------ approvals


def _approval(conn: psycopg.Connection[Any], w: World, rule: str, expired: bool = False) -> UUID:
    created = datetime.now(UTC) - timedelta(hours=2 if expired else 0)
    expires = created + timedelta(hours=1)
    return scalar(  # type: ignore[no-any-return]
        conn,
        "insert into app.approvals (org_id, task_id, action_summary, action_tier, approver_rule, created_at, expires_at) "
        "values (%s, %s, 'Open PR', 'reversible_write', %s, %s, %s) returning id",
        (w.acme.id, w.acme.ids["tasks"], rule, created, expires),
    )


def _decide(
    conn: psycopg.Connection[Any], approval: UUID, member: UUID | None, status: str = "approved"
) -> None:
    conn.execute(
        "update app.approvals set status = %s, decided_by = %s where id = %s", (status, member, approval)
    )


@pytest.mark.parametrize(
    ("rule", "decider", "allowed"),
    [
        ("requester_or_admin", "requester", True),
        ("requester_or_admin", "admin", True),
        ("requester_or_admin", "other", False),
        ("requester", "requester", True),
        ("requester", "admin", False),
        ("admin", "admin", True),
        ("admin", "requester", False),
    ],
)
def test_approval_decider_rules(world: World, rule: str, decider: str, allowed: bool) -> None:
    with tx(world.url, APP_USER, world.acme.id) as conn:
        who = {
            "requester": world.acme.user,
            "admin": world.acme.admin,
            "other": membership(conn, world.acme.id, "quinn@acme.test"),
        }
        approval = _approval(conn, world, rule)
        if allowed:
            _decide(conn, approval, who[decider])
            assert scalar(conn, "select decided_at is not null from app.approvals where id = %s", (approval,))
        else:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                _decide(conn, approval, who[decider])


def test_removed_member_cannot_decide(world: World) -> None:
    with tx(world.url) as conn:
        approval = _approval(conn, world, "requester")
        conn.execute("update app.org_memberships set status = 'removed' where id = %s", (world.acme.user,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _decide(conn, approval, world.acme.user)


def test_expired_approval_cannot_be_decided(world: World) -> None:
    with tx(world.url, APP_USER, world.acme.id) as conn:
        approval = _approval(conn, world, "requester_or_admin", expired=True)
        with pytest.raises(psycopg.errors.CheckViolation, match="expired"):
            _decide(conn, approval, world.acme.admin)


def test_decisions_are_final_and_need_a_decider(world: World) -> None:
    with tx(world.url, APP_USER, world.acme.id) as conn:
        approval = _approval(conn, world, "requester_or_admin")
        with pytest.raises(psycopg.errors.CheckViolation, match="needs decided_by"):
            _decide(conn, approval, None)
    with tx(world.url, APP_USER, world.acme.id) as conn:
        approval = _approval(conn, world, "requester_or_admin")
        _decide(conn, approval, world.acme.user)
        with pytest.raises(psycopg.errors.CheckViolation, match="already approved"):
            _decide(conn, approval, world.acme.admin, "rejected")


# ------------------------------------------------------------------ schedules


SCHEDULE = (
    "insert into app.schedules (org_id, owner_membership_id, name, instruction, cron, timezone, delivery_type, "
    "delivery_channel_id, status, pause_reason) values (%s, %s, 'S', 'I', %s, %s, %s, %s, %s, %s)"
)


@pytest.mark.parametrize(
    ("cron", "timezone", "delivery", "channel", "status", "reason"),
    [
        ("0 9 * * *", "Mars/Olympus", "dm", False, "paused", "admin"),
        ("0 9 * *", "UTC", "dm", False, "paused", "admin"),
        ("0 9 * * *", "UTC", "dm", True, "paused", "admin"),
        ("0 9 * * *", "UTC", "channel", False, "paused", "admin"),
        ("0 9 * * *", "UTC", "dm", False, "paused", None),
        ("0 9 * * *", "UTC", "dm", False, "active", "admin"),
    ],
    ids=[
        "bad_timezone",
        "bad_cron",
        "dm_with_channel",
        "channel_without_channel",
        "pause_without_reason",
        "reason_while_active",
    ],
)
def test_schedule_checks(
    world: World, cron: str, timezone: str, delivery: str, channel: bool, status: str, reason: str | None
) -> None:
    a = world.acme
    with tx(world.url, APP_USER, a.id) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            SCHEDULE, (a.id, a.user, cron, timezone, delivery, a.channel if channel else None, status, reason)
        )


def test_schedule_quota_per_org(world: World) -> None:
    a = world.acme
    with tx(world.url, APP_USER, a.id) as conn:
        conn.execute("update app.org_settings set max_schedules = 2")
        conn.execute(SCHEDULE, (a.id, a.user, "0 9 * * *", "UTC", "dm", None, "active", None))  # 2nd active
        with pytest.raises(psycopg.errors.CheckViolation, match="limit of 2"):
            conn.execute(SCHEDULE, (a.id, a.user, "0 9 * * *", "UTC", "dm", None, "active", None))
    with tx(world.url, APP_USER, a.id) as conn:
        conn.execute("update app.org_settings set max_schedules = 1")
        conn.execute(
            SCHEDULE, (a.id, a.user, "0 9 * * *", "UTC", "dm", None, "paused", "admin")
        )  # paused is free
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "update app.schedules set status = 'active', pause_reason = null where status = 'paused'"
            )


@pytest.mark.parametrize("change", ["is_allowlisted = false", "archived = true"])
def test_losing_the_delivery_channel_pauses_schedules(world: World, change: str) -> None:
    a = world.acme
    with tx(world.url, APP_USER, a.id) as conn:
        conn.execute(
            sql.SQL("update app.channels set {} where id = %s").format(sql.SQL(change)), (a.channel,)
        )
        assert conn.execute(
            "select status, pause_reason from app.schedules where id = %s", (a.ids["schedules"],)
        ).fetchone() == ("paused", "access_lost")


def test_removing_a_member_revokes_links_pauses_schedules_closes_sessions(world: World) -> None:
    a = world.acme
    with tx(world.url) as conn:
        conn.execute("update app.org_memberships set status = 'removed' where id = %s", (a.user,))
        assert (
            scalar(conn, "select status from app.connections where id = %s", (a.ids["user_connection"],))
            == "revoked"
        )
        assert (
            scalar(conn, "select status from app.connections where id = %s", (a.ids["connections"],))
            == "active"
        )  # org connection untouched
        assert conn.execute(
            "select status, pause_reason from app.schedules where id = %s", (a.ids["schedules"],)
        ).fetchone() == ("paused", "owner_removed")
        assert scalar(conn, "select status from app.sessions where id = %s", (a.ids["sessions"],)) == "closed"
        assert scalar(conn, "select removed_at is not null from app.org_memberships where id = %s", (a.user,))


# ------------------------------------------------------------------ artifacts and usage


def test_artifact_retention_defaults_from_org_settings(world: World) -> None:
    for org, days in ((world.acme, 90), (world.globex, 30)):
        with tx(world.url, APP_USER, org.id) as conn:
            delta = scalar(
                conn,
                "insert into app.artifacts (org_id, task_id, kind, content) values (%s, %s, 'summary', '{}') "
                "returning retain_until - now()",
                (org.id, org.ids["tasks"]),
            )
            assert abs(delta - timedelta(days=days)) < timedelta(minutes=1)


@pytest.mark.parametrize(
    ("content", "ref"), [(Jsonb({"a": 1}), "s3://bucket/key"), (None, None)], ids=["both", "neither"]
)
def test_artifact_has_exactly_one_body(world: World, content: Any, ref: str | None) -> None:
    with tx(world.url, APP_USER, world.acme.id) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "insert into app.artifacts (org_id, task_id, kind, content, storage_ref) values (%s, %s, 'diff', %s, %s)",
            (world.acme.id, world.acme.ids["tasks"], content, ref),
        )


def test_usage_rolls_up_per_member_and_for_system_usage(world: World) -> None:
    a = world.acme
    with tx(world.url, APP_USER, a.id) as conn:
        quinn = membership(conn, a.id, "quinn@acme.test")
        for member, tokens in ((quinn, 10), (quinn, 5), (None, 7), (None, 3)):
            conn.execute(
                "insert into app.usage_events (org_id, membership_id, kind, input_tokens, output_tokens, cost) "
                "values (%s, %s, 'llm', %s, 0, 0.5)",
                (a.id, member, tokens),
            )
        rows = dict(
            conn.execute(
                "select membership_id, tokens from app.usage_daily where day = (now() at time zone 'UTC')::date"
            ).fetchall()
        )
    assert rows[quinn] == 15
    assert rows[None] == 10  # one row for system usage (nulls not distinct)


# ------------------------------------------------------------------ uniqueness and approval of custom MCP


def test_idempotency_keys_are_unique_per_org_only(world: World) -> None:
    # The world already holds 'idem-key-0001' in both orgs; a repeat inside one org is refused.
    with tx(world.url, APP_USER, world.acme.id) as conn, pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "insert into app.external_actions (org_id, task_id, integration_key, operation, idempotency_key) "
            "values (%s, %s, 'jira', 'create_issue', 'idem-key-0001')",
            (world.acme.id, world.acme.ids["tasks"]),
        )


def test_one_active_org_connection_per_tool(world: World) -> None:
    a = world.acme
    insert = "insert into app.connections (org_id, integration_key, scope) values (%s, 'jira', 'org')"
    with tx(world.url, APP_USER, a.id) as conn, pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(insert, (a.id,))
    with tx(world.url, APP_USER, a.id) as conn:
        conn.execute("update app.connections set status = 'revoked' where id = %s", (a.ids["connections"],))
        conn.execute(insert, (a.id,))  # a new active one is fine once the old one is revoked


def test_custom_mcp_needs_approval(world: World) -> None:
    a = world.acme
    with tx(world.url, APP_USER, a.id) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "insert into app.org_integrations (org_id, integration_key, kind, enabled) values (%s, 'my_mcp', 'custom_mcp', true)",
            (a.id,),
        )
    with tx(world.url, APP_USER, a.id) as conn:
        conn.execute(
            "insert into app.org_integrations (org_id, integration_key, kind, enabled, approved_by, approved_at) "
            "values (%s, 'my_mcp', 'custom_mcp', true, %s, now())",
            (a.id, a.admin),
        )


# ------------------------------------------------------------------ deletion and ids


def test_deleting_an_org_removes_its_data_and_leaves_others_intact(world: World) -> None:
    from tests.world import TENANT_TABLES

    def counts(conn: psycopg.Connection[Any], org: UUID) -> dict[str, int]:
        return {
            t: scalar(
                conn,
                sql.SQL("select count(*) from app.{} where org_id = %s").format(sql.Identifier(t)),
                (org,),
            )
            for t in TENANT_TABLES
        }

    with tx(world.url) as conn:
        globex_before = counts(conn, world.globex.id)
        conn.execute("delete from app.orgs where id = %s", (world.acme.id,))
        assert set(counts(conn, world.acme.id).values()) == {0}
        assert counts(conn, world.globex.id) == globex_before
        # Global users remain (they may belong to other orgs); Frank still has his Globex membership.
        assert scalar(conn, "select count(*) from app.users where email = 'frank@both.test'") == 1


def test_uuid_v7_is_version_7_and_time_ordered(world: World) -> None:
    with tx(world.url) as conn:
        first = [r[0] for r in conn.execute("select app.uuid_v7() from generate_series(1, 50)")]
        conn.execute("select pg_sleep(0.005)")
        second = [r[0] for r in conn.execute("select app.uuid_v7() from generate_series(1, 50)")]
    for u in first + second:
        assert u.version == 7
        assert u.variant == uuid.RFC_4122
    assert max(first) < min(second)
    assert len(set(first + second)) == 100

"""Migration 002: identity sync functions (Clerk mirror) and console resolvers."""

from typing import Any

import psycopg
import pytest

from tests.world import World, scalar, tx

SYNC = "app_identity_sync"


def call(conn: psycopg.Connection[Any], fn: str, *args: Any) -> Any:
    placeholders = ", ".join(["%s"] * len(args))
    return scalar(conn, f"select app.{fn}({placeholders})", args)


def membership(conn: psycopg.Connection[Any], clerk_org: str, email: str) -> tuple[Any, ...] | None:
    return conn.execute(
        "select m.status, m.privilege, u.clerk_user_id, m.joined_at is not null from app.org_memberships m "
        "join app.users u on u.id = m.user_id join app.orgs o on o.id = m.org_id "
        "where o.clerk_org_id = %s and lower(u.email) = lower(%s)",
        (clerk_org, email),
    ).fetchone()


# ------------------------------------------------------------------ orgs and users


def test_new_signup_creates_org_user_and_admin_membership(world: World) -> None:
    with tx(world.url, SYNC) as conn:
        org = call(conn, "identity_upsert_org", "org_new1", "New Co")
        assert call(conn, "identity_upsert_org", "org_new1", "New Co") == org  # idempotent
        call(
            conn,
            "identity_upsert_membership",
            "org_new1",
            "New Co",
            "user_new1",
            "founder@new.test",
            "Fay Founder",
            "org:admin",
        )
        assert membership(conn, "org_new1", "founder@new.test") == ("active", "admin", "user_new1", True)
        conn.execute("reset role")
        assert scalar(conn, "select count(*) from app.org_settings where org_id = %s", (org,)) == 1
        actions = {r[0] for r in conn.execute("select action from app.audit_log where org_id = %s", (org,))}
        assert {"identity.org_created", "identity.membership_active"} <= actions


def test_org_name_updates_but_never_blanks(world: World) -> None:
    with tx(world.url, SYNC) as conn:
        call(conn, "identity_upsert_org", "org_acme", "Acme Renamed")
        call(conn, "identity_upsert_org", "org_acme", "  ")
        conn.execute("reset role")
        assert scalar(conn, "select name from app.orgs where clerk_org_id = 'org_acme'") == "Acme Renamed"


def test_first_signup_links_the_invited_user_by_email(world: World) -> None:
    with tx(world.url, SYNC) as conn:
        carol_before = membership(conn, "org_acme", "carol@acme.test")
        assert carol_before is not None
        assert carol_before[0] == "invited"
        assert carol_before[2] is None
        call(
            conn,
            "identity_upsert_membership",
            "org_acme",
            "Acme Corp",
            "user_carol",
            "CAROL@acme.test",
            "Carol",
            "org:member",
        )
        assert membership(conn, "org_acme", "carol@acme.test") == ("active", "user", "user_carol", True)
        conn.execute("reset role")
        assert scalar(conn, "select count(*) from app.users where lower(email) = 'carol@acme.test'") == 1


def test_email_change_that_collides_keeps_the_old_email(world: World) -> None:
    with tx(world.url, SYNC) as conn:
        call(conn, "identity_upsert_user", "user_bob", "alice@acme.test", "Bob Renamed")
        conn.execute("reset role")
        assert conn.execute(
            "select email, name from app.users where clerk_user_id = 'user_bob'"
        ).fetchone() == ("bob@acme.test", "Bob Renamed")


def test_email_change_updates_the_user(world: World) -> None:
    with tx(world.url, SYNC) as conn:
        call(conn, "identity_upsert_user", "user_bob", "robert@acme.test", None)
        conn.execute("reset role")
        assert (
            scalar(conn, "select email from app.users where clerk_user_id = 'user_bob'") == "robert@acme.test"
        )


@pytest.mark.parametrize(("clerk_id", "email"), [("", "x@y.test"), ("user_x", ""), (None, "x@y.test")])
def test_user_requires_id_and_email(world: World, clerk_id: str | None, email: str) -> None:
    with tx(world.url, SYNC) as conn, pytest.raises(psycopg.errors.CheckViolation):
        call(conn, "identity_upsert_user", clerk_id, email, "x")


# ------------------------------------------------------------------ invitations


def test_invite_creates_the_person_before_signup(world: World) -> None:
    with tx(world.url, SYNC) as conn:
        call(conn, "identity_invite", "org_acme", "newhire@acme.test", "org:member", "user_alice")
        assert membership(conn, "org_acme", "newhire@acme.test") == ("invited", "user", None, False)
        conn.execute("reset role")
        inviter = scalar(
            conn,
            "select u.email from app.org_memberships m join app.users iu on iu.id = m.user_id "
            "join app.org_memberships im on im.id = m.invited_by join app.users u on u.id = im.user_id "
            "where iu.email = 'newhire@acme.test'",
        )
        assert inviter == "alice@acme.test"


def test_inviting_an_active_member_changes_nothing(world: World) -> None:
    with tx(world.url, SYNC) as conn:
        call(conn, "identity_invite", "org_acme", "alice@acme.test", "org:member", None)
        assert membership(conn, "org_acme", "alice@acme.test") == ("active", "admin", "user_alice", True)


def test_removed_member_can_be_invited_again(world: World) -> None:
    with tx(world.url, SYNC) as conn:
        call(conn, "identity_remove_membership", "org_acme", "user_bob")
        assert membership(conn, "org_acme", "bob@acme.test")[0] == "removed"  # type: ignore[index]
        call(conn, "identity_invite", "org_acme", "bob@acme.test", "org:admin", "user_alice")
        assert membership(conn, "org_acme", "bob@acme.test")[:2] == ("invited", "admin")  # type: ignore[index]


@pytest.mark.parametrize(
    ("org", "email", "error"),
    [
        ("org_nope", "a@b.test", psycopg.errors.ForeignKeyViolation),
        ("org_acme", "not-an-email", psycopg.errors.CheckViolation),
    ],
)
def test_invite_validation(world: World, org: str, email: str, error: type[Exception]) -> None:
    with tx(world.url, SYNC) as conn, pytest.raises(error):
        call(conn, "identity_invite", org, email, "org:member", None)


def test_revoking_an_invitation_removes_only_invited_memberships(world: World) -> None:
    with tx(world.url, SYNC) as conn:
        assert call(conn, "identity_revoke_invitation", "org_acme", "carol@acme.test") is not None
        assert membership(conn, "org_acme", "carol@acme.test")[0] == "removed"  # type: ignore[index]
        assert call(conn, "identity_revoke_invitation", "org_acme", "bob@acme.test") is None
        assert membership(conn, "org_acme", "bob@acme.test")[0] == "active"  # type: ignore[index]


# ------------------------------------------------------------------ privilege and removal


def test_promote_and_demote(world: World) -> None:
    with tx(world.url, SYNC) as conn:
        assert call(conn, "identity_set_privilege", "org_acme", "user_bob", "org:admin") is not None
        assert membership(conn, "org_acme", "bob@acme.test")[1] == "admin"  # type: ignore[index]
        call(conn, "identity_set_privilege", "org_acme", "user_alice", "org:member")  # bob is still admin
        assert membership(conn, "org_acme", "alice@acme.test")[1] == "user"  # type: ignore[index]


def test_demoting_or_removing_the_last_admin_is_refused(world: World) -> None:
    for fn, args in (
        ("identity_set_privilege", ("org_globex", "user_dave", "org:member")),
        ("identity_remove_membership", ("org_globex", "user_dave")),
    ):
        with (
            tx(world.url, SYNC) as conn,
            pytest.raises(psycopg.errors.CheckViolation, match="at least one active admin"),
        ):
            call(conn, fn, *args)
    with tx(world.url, SYNC) as conn, pytest.raises(psycopg.errors.CheckViolation):
        call(
            conn,
            "identity_upsert_membership",
            "org_globex",
            "Globex Inc",
            "user_dave",
            "dave@globex.test",
            "Dave",
            "org:member",
        )


def test_unknown_membership_returns_null(world: World) -> None:
    with tx(world.url, SYNC) as conn:
        assert call(conn, "identity_set_privilege", "org_acme", "user_dave", "org:admin") is None
        assert call(conn, "identity_remove_membership", "org_acme", "user_dave") is None


def test_removal_triggers_the_cascade(world: World) -> None:
    with tx(world.url, SYNC) as conn:
        call(conn, "identity_remove_membership", "org_acme", "user_bob")
        conn.execute("reset role")
        assert (
            scalar(
                conn, "select status from app.connections where id = %s", (world.acme.ids["user_connection"],)
            )
            == "revoked"
        )
        assert (
            scalar(conn, "select status from app.schedules where id = %s", (world.acme.ids["schedules"],))
            == "paused"
        )


def test_deleted_user_is_disabled_but_never_leaves_an_org_without_admin(world: World) -> None:
    with tx(world.url, SYNC) as conn:
        call(
            conn,
            "identity_upsert_membership",
            "org_globex",
            "Globex Inc",
            "user_frank",
            "frank@both.test",
            "Frank",
            "org:admin",
        )
        call(conn, "identity_set_privilege", "org_globex", "user_dave", "org:member")
        # Frank is now Globex's only admin and a plain member of Acme.
        assert call(conn, "identity_disable_user", "user_frank") == 1
        assert membership(conn, "org_acme", "frank@both.test")[0] == "removed"  # type: ignore[index]
        assert membership(conn, "org_globex", "frank@both.test")[0] == "active"  # type: ignore[index]
        conn.execute("reset role")
        assert scalar(conn, "select status from app.users where clerk_user_id = 'user_frank'") == "disabled"
        assert (
            scalar(
                conn,
                "select count(*) from app.audit_log where action = 'identity.last_admin_kept' and org_id = %s",
                (world.globex.id,),
            )
            == 1
        )


def test_deleted_org_goes_to_offboarding_and_stops_routing(world: World) -> None:
    with tx(world.url, SYNC) as conn:
        call(conn, "identity_offboard_org", "org_globex")
        assert call(conn, "identity_offboard_org", "org_globex") is None  # idempotent
        conn.execute("reset role")
        assert scalar(conn, "select status from app.orgs where clerk_org_id = 'org_globex'") == "offboarding"
        assert conn.execute("select * from app.resolve_workspace('slack', 'T0GLOBEX')").fetchall() == []


# ------------------------------------------------------------------ console resolvers


def test_resolve_console_identity(world: World) -> None:
    with tx(world.url, "app_user") as conn:  # no org context needed
        row = conn.execute(
            "select org_id, privilege, membership_status, user_status, org_status from app.resolve_console_identity('user_alice', 'org_acme')"
        ).fetchone()
        assert row == (world.acme.id, "admin", "active", "active", "active")
        assert (
            conn.execute("select * from app.resolve_console_identity('user_alice', 'org_globex')").fetchall()
            == []
        )
        assert (
            conn.execute("select * from app.resolve_console_identity('user_nobody', 'org_acme')").fetchall()
            == []
        )


def test_list_console_orgs_shows_only_active_memberships(world: World) -> None:
    with tx(world.url, "app_user") as conn:
        assert [r[2] for r in conn.execute("select * from app.list_console_orgs('user_frank')")] == [
            "Acme Corp",
            "Globex Inc",
        ]
    with tx(world.url) as conn:
        conn.execute(
            "update app.org_memberships set status = 'removed' where id in (select m.id from app.org_memberships m join app.users u on u.id = m.user_id where u.clerk_user_id = 'user_frank' and m.org_id = %s)",
            (world.acme.id,),
        )
        conn.execute("set local role app_user")
        assert [r[2] for r in conn.execute("select * from app.list_console_orgs('user_frank')")] == [
            "Globex Inc"
        ]


# ------------------------------------------------------------------ grants


IDENTITY_CALLS = [
    "app.identity_upsert_org('org_x', 'X')",
    "app.identity_upsert_user('user_x', 'x@x.test', 'X')",
    "app.identity_upsert_membership('org_acme', 'Acme', 'user_x', 'x@x.test', 'X', 'org:admin')",
    "app.identity_invite('org_acme', 'x@x.test', 'org:admin', null)",
    "app.identity_set_privilege('org_acme', 'user_bob', 'org:admin')",
    "app.identity_remove_membership('org_acme', 'user_bob')",
    "app.identity_revoke_invitation('org_acme', 'carol@acme.test')",
    "app.identity_disable_user('user_bob')",
    "app.identity_offboard_org('org_acme')",
    "app.identity_audit(null, 'x', 'y', '{}')",
]


@pytest.mark.parametrize("role", ["app_user", "app_vault", "app_maintenance"])
@pytest.mark.parametrize("call_sql", IDENTITY_CALLS)
def test_only_identity_sync_may_change_identity(world: World, role: str, call_sql: str) -> None:
    with tx(world.url, role, world.acme.id) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(f"select {call_sql}")


def test_identity_sync_cannot_call_internal_audit_helper(world: World) -> None:
    with tx(world.url, SYNC) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("select app.identity_audit(null, 'x', 'y', '{}')")


@pytest.mark.parametrize("role", ["app_identity_sync", "app_vault", "app_maintenance"])
def test_console_resolvers_are_for_the_api_only(world: World, role: str) -> None:
    with tx(world.url, role) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("select * from app.resolve_console_identity('user_alice', 'org_acme')")


# ------------------------------------------------------------------ out of order events


def test_a_late_invitation_event_cannot_undo_a_revocation(world: World) -> None:
    with tx(world.url, SYNC) as conn:
        conn.execute(
            "select app.identity_invite('org_acme', 'late@acme.test', 'org:member', null, now() - interval '10 seconds')"
        )
        conn.execute("select app.identity_revoke_invitation('org_acme', 'late@acme.test', now())")
        # The original "created" event arrives again (Svix retry) or late: older than the revocation.
        conn.execute(
            "select app.identity_invite('org_acme', 'late@acme.test', 'org:member', null, now() - interval '10 seconds')"
        )
        assert membership(conn, "org_acme", "late@acme.test")[0] == "removed"  # type: ignore[index]
        # A genuinely new invitation (newer event) is applied.
        conn.execute(
            "select app.identity_invite('org_acme', 'late@acme.test', 'org:admin', null, now() + interval '1 second')"
        )
        assert membership(conn, "org_acme", "late@acme.test")[:2] == ("invited", "admin")  # type: ignore[index]


def test_stale_membership_events_are_ignored(world: World) -> None:
    with tx(world.url, SYNC) as conn:
        conn.execute("select app.identity_remove_membership('org_acme', 'user_bob', now())")
        stale = "now() - interval '5 seconds'"
        conn.execute(
            f"select app.identity_upsert_membership('org_acme', 'Acme Corp', 'user_bob', 'bob@acme.test', 'Bob', 'org:member', {stale})"
        )
        assert membership(conn, "org_acme", "bob@acme.test")[0] == "removed"  # type: ignore[index]
        conn.execute(f"select app.identity_set_privilege('org_acme', 'user_quinn', 'org:admin', {stale})")
        conn.execute("select app.identity_set_privilege('org_acme', 'user_quinn', 'org:admin', now())")
        conn.execute(f"select app.identity_set_privilege('org_acme', 'user_quinn', 'org:member', {stale})")
        # The older demotion lost.
        assert membership(conn, "org_acme", "quinn@acme.test")[1] == "admin"  # type: ignore[index]
        conn.execute(f"select app.identity_remove_membership('org_acme', 'user_quinn', {stale})")
        assert membership(conn, "org_acme", "quinn@acme.test")[0] == "active"  # type: ignore[index]


def test_the_staleness_helper_is_internal(world: World) -> None:
    with (
        tx(world.url, "app_user", world.acme.id) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("select app.identity_is_stale(gen_random_uuid(), gen_random_uuid(), now())")

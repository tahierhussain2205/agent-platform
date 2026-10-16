"""Schedule bookkeeping (migration 007): revisions for the Temporal mirror, pause stamps for the admin
notification, and the maintenance function that finds orgs needing attention."""

from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg import sql

from tests.world import Org, World, scalar


def _schedule(conn: psycopg.Connection[Any], org: Org, name: str = "Report") -> UUID:
    return scalar(  # type: ignore[no-any-return]
        conn,
        "insert into app.schedules (org_id, owner_membership_id, name, instruction, cron, delivery_type, "
        "delivery_channel_id) values (%s, %s, %s, 'Summarize', '0 9 * * *', 'channel', %s) returning id",
        (org.id, org.user, name, org.channel),
    )


def _row(conn: psycopg.Connection[Any], schedule: UUID) -> tuple[Any, ...]:
    row = conn.execute(
        "select revision, synced_revision, paused_at is not null, pause_notified_at is not null "
        "from app.schedules where id = %s",
        (schedule,),
    ).fetchone()
    assert row is not None
    return tuple(row)


def test_revision_moves_only_when_the_schedule_changes(world: World) -> None:
    with psycopg.connect(world.url) as conn:
        s = _schedule(conn, world.acme)
        assert _row(conn, s) == (1, None, False, False)
        conn.execute(
            "update app.schedules set synced_revision = revision, last_run_at = now() where id = %s", (s,)
        )
        assert _row(conn, s) == (1, 1, False, False)  # bookkeeping only
        conn.execute("update app.schedules set cron = '30 9 * * *' where id = %s", (s,))
        assert _row(conn, s)[:2] == (2, 1)  # needs syncing again
        conn.execute('update app.schedules set params = \'{"project": "PAY"}\' where id = %s', (s,))
        conn.execute("update app.schedules set owner_membership_id = %s where id = %s", (world.acme.admin, s))
        assert _row(conn, s)[0] == 4
        conn.execute("update app.schedules set revision = 1 where id = %s", (s,))  # nobody sets it by hand
        assert _row(conn, s)[0] == 4
        conn.execute("update app.schedules set cron = cron where id = %s", (s,))  # no change
        assert _row(conn, s)[0] == 4
        conn.rollback()


def test_pausing_stamps_and_resuming_clears(world: World) -> None:
    with psycopg.connect(world.url) as conn:
        s = _schedule(conn, world.acme)
        conn.execute(
            "update app.schedules set status = 'paused', pause_reason = 'access_lost' where id = %s", (s,)
        )
        assert _row(conn, s)[2:] == (True, False)
        conn.execute("update app.schedules set pause_notified_at = now() where id = %s", (s,))
        assert _row(conn, s)[2:] == (True, True)
        first = scalar(conn, "select revision from app.schedules where id = %s", (s,))
        conn.execute("update app.schedules set pause_reason = 'owner_removed' where id = %s", (s,))
        assert _row(conn, s)[2:] == (True, False)  # a new reason is a new pause to tell admins about
        assert scalar(conn, "select revision from app.schedules where id = %s", (s,)) == first + 1
        conn.execute("update app.schedules set status = 'active', pause_reason = null where id = %s", (s,))
        assert _row(conn, s)[2:] == (False, False)
        conn.rollback()


def test_trigger_pauses_are_stamped_too(world: World) -> None:
    """The existing triggers (owner removed, delivery channel off the allowlist) pause through
    plain updates, so they get stamped and need notifying."""
    with psycopg.connect(world.url) as conn:
        s = _schedule(conn, world.acme)
        conn.execute("update app.channels set is_allowlisted = false where id = %s", (world.acme.channel,))
        row = conn.execute(
            "select status, pause_reason, paused_at is not null, revision from app.schedules where id = %s",
            (s,),
        ).fetchone()
        assert row == ("paused", "access_lost", True, 2)
        conn.rollback()


def test_maintenance_lists_orgs_needing_attention(world: World) -> None:
    with psycopg.connect(world.url) as conn:
        conn.execute("update app.schedules set synced_revision = revision")
        conn.execute(
            "update app.schedules set status = 'paused', pause_reason = 'admin', synced_revision = revision + 1 "
            "where org_id = %s",
            (world.globex.id,),
        )
        conn.execute(
            "update app.schedules set synced_revision = revision where org_id = %s", (world.globex.id,)
        )
        conn.execute("set local role app_maintenance")
        assert (
            conn.execute("select org_id from app.maintenance_schedule_orgs()").fetchall() == []
        )  # admin pause
        conn.execute("reset role")
        s = _schedule(conn, world.acme)  # never synced
        conn.execute("set local role app_maintenance")
        assert conn.execute("select org_id from app.maintenance_schedule_orgs()").fetchall() == [
            (world.acme.id,)
        ]
        conn.execute("reset role")
        conn.execute(
            "update app.schedules set synced_revision = revision + 1, status = 'paused', "
            "pause_reason = 'owner_removed' where id = %s",
            (s,),
        )
        conn.execute("update app.schedules set synced_revision = revision where id = %s", (s,))
        conn.execute("set local role app_maintenance")
        assert conn.execute("select org_id from app.maintenance_schedule_orgs()").fetchall() == [
            (world.acme.id,)
        ]
        conn.rollback()


@pytest.mark.parametrize("role", ["app_user", "app_vault", "app_identity_sync"])
def test_schedule_attention_is_for_maintenance_only(world: World, role: str) -> None:
    with psycopg.connect(world.url) as conn:
        conn.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select * from app.maintenance_schedule_orgs()")

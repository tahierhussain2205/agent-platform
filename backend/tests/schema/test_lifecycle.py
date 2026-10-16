"""Data lifecycle (plan phase 13): retention per org, old partitions, and offboarding across every
store. Each test gets its own database (these tests delete orgs) and runs the jobs as the
maintenance role, which has no BYPASSRLS (B-010)."""

import asyncio
import os
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from psycopg import sql

from app import kv
from app.db import roles
from app.db.roles import LoginUser, ensure_login_user
from app.lifecycle import apply_retention, offboard, offboarding_job
from app.settings import Settings
from app.storage import ObjectNotFoundError, ObjectStore
from tests.conftest import ADMIN_URL, throwaway_database
from tests.schema.test_db_roles import PASSWORD, _url_for
from tests.stack import kv_url
from tests.world import World, create_world

S3 = os.environ.get("TEST_S3_ENDPOINT", "http://localhost:8333")


@pytest.fixture
def fresh(postgres: None) -> Iterator[World]:
    with throwaway_database("lworld") as url:
        yield create_world(url)


@pytest.fixture
def maintenance(fresh: World, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    user = LoginUser("maintenance", f"t{uuid.uuid4().hex[:6]}_maintenance", "app_maintenance")
    with psycopg.connect(fresh.url, autocommit=True) as conn:
        ensure_login_user(conn, user, PASSWORD)
    monkeypatch.setitem(roles.LOGIN_USERS, "maintenance", user)
    yield _url_for(user.username, PASSWORD, fresh.url.rsplit("/", 1)[1])
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(user.username)))


def settings(**kw: Any) -> Settings:
    return Settings(redis_url=kv_url(11), s3_bucket="artifacts", s3_endpoint_url=S3,
                    s3_access_key_id="local-access-key", s3_secret_access_key="local-secret-key", **kw)  # fmt: skip


def q(world: World, statement: str, params: tuple[Any, ...] = ()) -> Any:
    with psycopg.connect(world.url) as conn:  # the owner: sees every org
        cur = conn.execute(statement, params)
        rows = cur.fetchall() if cur.description else None
        conn.commit()
        return rows


def task(world: World, org: Any, *, days_ago: int, status: str = "completed", session: Any = None) -> Any:
    finished = (
        "now() - make_interval(days => %s)" if status in ("completed", "failed", "cancelled") else "null"
    )
    return q(world, f"insert into app.tasks (org_id, session_id, requested_by, trigger, model_tier, status, created_at, "
                    f"finished_at) values (%s, %s, %s, 'mention', 'standard', %s, now() - make_interval(days => %s), "
                    f"{finished}) returning id",
             (org.id, session, org.user, status, days_ago, *((days_ago,) if finished != "null" else ())))[0][0]  # fmt: skip


def artifact(world: World, org: Any, task_id: Any, *, expired: bool, storage_ref: str | None = None) -> Any:
    return q(world, "insert into app.artifacts (org_id, task_id, kind, content, storage_ref, retain_until) values "
                    "(%s, %s, 'summary', %s, %s, now() + make_interval(days => %s)) returning id",
             (org.id, task_id, None if storage_ref else '{"a": 1}', storage_ref, -1 if expired else 30))[0][0]  # fmt: skip


def session(world: World, org: Any, *, idle_days: int) -> Any:
    return q(world, "insert into app.sessions (org_id, workspace_id, external_channel_id, external_thread_id, "
                    "last_activity_at) values (%s, %s, 'C1', %s, now() - make_interval(days => %s)) returning id",
             (org.id, org.workspace, f"{uuid.uuid4().int % 10**10}.0001", idle_days))[0][0]  # fmt: skip


def run(dsn: str, job: Any) -> Any:
    with psycopg.connect(dsn, autocommit=True, prepare_threshold=None) as conn, conn.transaction():
        return job(conn)


def test_retention_deletes_what_is_past_each_orgs_retention_and_nothing_else(
    fresh: World, maintenance: str
) -> None:
    acme, globex = fresh.acme, fresh.globex
    q(fresh, "update app.org_settings set retention_days = 365 where org_id = %s", (globex.id,))
    old_done = task(fresh, acme, days_ago=100)
    recent_done = task(fresh, acme, days_ago=10)
    old_running = task(fresh, acme, days_ago=100, status="running")
    their_old = task(fresh, globex, days_ago=100)  # within globex's longer retention
    under_old = artifact(fresh, acme, old_done, expired=False)
    expired = artifact(fresh, acme, recent_done, expired=True)
    kept = artifact(fresh, acme, recent_done, expired=False)
    idle = session(fresh, acme, idle_days=120)
    busy = session(fresh, acme, idle_days=120)
    task(fresh, acme, days_ago=120, status="running", session=busy)
    done = run(maintenance, lambda conn: apply_retention(conn, settings(), store=object()))
    assert (done.tasks, done.artifacts, done.sessions) == (1, 1, 1)
    tasks = {r[0] for r in q(fresh, "select id from app.tasks")}
    assert old_done not in tasks
    assert {recent_done, old_running, their_old} <= tasks
    artifacts = {r[0] for r in q(fresh, "select id from app.artifacts")}
    assert under_old not in artifacts  # went with its task
    assert expired not in artifacts
    assert kept in artifacts
    sessions = {r[0] for r in q(fresh, "select id from app.sessions")}
    assert idle not in sessions
    assert busy in sessions  # a task there is still running


def test_retention_deletes_objects_first_and_keeps_rows_whose_object_stayed(
    fresh: World, maintenance: str
) -> None:
    acme = fresh.acme
    store = ObjectStore("artifacts", endpoint_url=S3, access_key_id="local-access-key",
                        secret_access_key="local-secret-key")  # fmt: skip
    org = uuid.UUID(str(acme.id))
    good = asyncio.run(store.put(org, "diff", b"old diff"))
    stuck = f"s3://artifacts/tenants/{acme.id}/diff/{uuid.uuid4()}"  # deleting it fails below
    owner = task(fresh, acme, days_ago=10)
    a_good = artifact(fresh, acme, owner, expired=True, storage_ref=good)
    a_stuck = artifact(fresh, acme, owner, expired=True, storage_ref=stuck)

    class Store:
        async def delete(self, org_id: uuid.UUID, ref: str) -> None:
            if ref == stuck:
                raise RuntimeError("storage is down")
            await store.delete(org_id, ref)

    done = run(maintenance, lambda conn: apply_retention(conn, settings(), store=Store()))
    assert (done.objects, done.failed_objects) == (1, 1)
    left = {r[0] for r in q(fresh, "select id from app.artifacts")}
    assert a_good not in left
    assert a_stuck in left  # retried next run: its object must not be orphaned
    with pytest.raises(ObjectNotFoundError):
        asyncio.run(store.get(org, good))


def test_old_partitions_are_dropped_and_recent_ones_kept(fresh: World, maintenance: str) -> None:
    for parent in ("usage_events", "audit_log"):
        q(fresh, sql.SQL("create table app.{} partition of app.{} for values from ('2020-01-01') to ('2020-02-01')")
          .format(sql.Identifier(f"{parent}_2020_01"), sql.Identifier(parent)).as_string())  # fmt: skip
    done = run(maintenance, lambda conn: apply_retention(conn, settings(), store=object()))
    assert done.partitions == 2
    names = {
        r[0] for r in q(fresh, "select relname from pg_class where relname ~ '^(usage_events|audit_log)_'")
    }
    assert not {"usage_events_2020_01", "audit_log_2020_01"} & names
    assert {"usage_events_default", "audit_log_default"} <= names
    with psycopg.connect(maintenance) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("select app.maintenance_drop_partitions('tasks', 13)")


def test_offboarding_waits_for_the_grace_period_then_deletes_the_org_everywhere(
    fresh: World, maintenance: str
) -> None:
    globex, acme = fresh.globex, fresh.acme
    cfg = settings()
    store = ObjectStore("artifacts", endpoint_url=S3, access_key_id="local-access-key",
                        secret_access_key="local-secret-key")  # fmt: skip

    async def seed_outside() -> None:
        client = kv.make_client(kv_url(11))
        for org in (globex.id, acme.id):
            await store.put(uuid.UUID(str(org)), "diff", b"x")
            await client.set(kv.tenant_key(uuid.UUID(str(org)), "probe"), "1")
        await client.aclose()

    asyncio.run(seed_outside())
    task(fresh, globex, days_ago=1)
    with psycopg.connect(maintenance, autocommit=True) as conn:
        assert conn.execute("select app.maintenance_begin_offboarding(%s)", (globex.id,)).fetchone() == (
            True,
        )
        assert conn.execute("select app.maintenance_begin_offboarding(%s)", (globex.id,)).fetchone() == (
            False,
        )
    stamped = q(fresh, "select offboarding_since > now() - interval '1 minute' from app.orgs where id = %s",
                (globex.id,))  # fmt: skip
    assert stamped == [(True,)]  # the grace period counts from here
    assert run(maintenance, offboarding_job(cfg)[1]) == "no org to offboard"  # within its grace period
    assert q(fresh, "select status from app.orgs where id = %s", (globex.id,)) == [("offboarding",)]
    q(fresh, "update app.orgs set offboarding_since = now() - interval '8 days' where id = %s", (globex.id,))
    detail = run(maintenance, offboarding_job(cfg)[1])
    assert detail.startswith(f"{globex.id}: deleted")
    # Not a row of globex anywhere; acme untouched.
    tables = [r[0] for r in q(fresh, "select table_name from information_schema.columns where table_schema = 'app' "
                                     "and column_name = 'org_id' and table_name not like '%%\\_20%%'")]  # fmt: skip
    for table in tables:
        count = q(fresh, sql.SQL("select count(*) from app.{} where org_id = %s").format(sql.Identifier(table))
                  .as_string(), (globex.id,))[0][0]  # fmt: skip
        assert count == 0, table
    assert q(fresh, "select count(*) from app.orgs where id = %s", (acme.id,)) == [(1,)]
    assert q(fresh, "select count(*) from app.tasks where org_id = %s", (acme.id,))[0][0] > 0
    emails = {r[0] for r in q(fresh, "select email from app.users")}
    assert "dave@globex.test" not in emails  # only in globex
    assert "frank@both.test" in emails  # also in acme

    async def outside() -> tuple[int, int, bool, bool]:
        client = kv.make_client(kv_url(11))
        theirs = len([k async for k in client.scan_iter(match=f"ap:t:{globex.id}:*")])
        ours = len([k async for k in client.scan_iter(match=f"ap:t:{acme.id}:*")])
        await client.aclose()
        mine = await store.delete_tenant(uuid.UUID(str(acme.id)))  # cleanup, and proof acme's were there
        return theirs, ours, mine > 0, False

    theirs, ours, acme_objects, _ = asyncio.run(outside())
    assert (theirs, ours, acme_objects) == (0, 1, True)
    assert asyncio.run(store.delete_tenant(uuid.UUID(str(globex.id)))) == 0  # already deleted


def test_an_active_org_is_never_offboarded(fresh: World, maintenance: str) -> None:
    result = run(maintenance, lambda conn: offboard(conn, settings(), fresh.acme.id, timedelta(0)))
    assert (result.deleted, result.notes) == (False, ["not offboarding: nothing deleted"])
    with psycopg.connect(maintenance) as conn:
        assert conn.execute(
            "select app.maintenance_offboard_org(%s, '0 days')", (fresh.acme.id,)
        ).fetchone() == (False,)
    assert q(fresh, "select status from app.orgs where id = %s", (fresh.acme.id,)) == [("active",)]


def test_self_service_orgs_start_in_shadow_mode(fresh: World) -> None:
    with psycopg.connect(fresh.url) as conn:
        org = conn.execute("select app.identity_upsert_org('org_selfserve', 'Self Serve')").fetchone()
        again = conn.execute("select app.identity_upsert_org('org_selfserve', 'Renamed')").fetchone()
        conn.commit()
    assert org is not None
    assert org == again
    assert q(fresh, "select shadow_mode from app.org_settings where org_id = %s", (org[0],)) == [(True,)]
    assert q(fresh, "select shadow_mode from app.org_settings where org_id = %s", (fresh.acme.id,)) == [
        (False,)
    ]


def test_only_the_maintenance_role_may_delete_orgs_or_partitions(fresh: World) -> None:
    from tests.world import tx

    for call in (
        "select app.maintenance_offboard_org(%s, '0 days')",
        "select app.maintenance_begin_offboarding(%s)",
    ):
        with (
            tx(fresh.url, role="app_user", org=fresh.acme.id) as conn,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            conn.execute(call, (fresh.acme.id,))
    with (
        tx(fresh.url, role="app_user", org=fresh.acme.id) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("select app.maintenance_drop_partitions('audit_log', 13)")
    assert q(fresh, "select status from app.orgs where id = %s", (fresh.acme.id,)) == [("active",)]

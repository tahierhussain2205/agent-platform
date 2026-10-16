"""Sandbox guarantees (migration 009): one sandbox per task and repository, a ready sandbox names its
provider id and base commit, destroyed means a destruction time, and only the maintenance role
lists expired sandboxes across orgs."""

from typing import Any

import psycopg
import pytest

from tests.world import World, scalar, tx

INSERT = (
    "insert into app.sandboxes (org_id, task_id, repository, status, provider_id, base_sha, expires_at, "
    "destroyed_at) values (%(org)s, %(task)s, %(repo)s, %(status)s, %(provider)s, %(sha)s, "
    "now() + %(ttl)s::interval, %(destroyed)s)"
)


def row(world: World, **overrides: Any) -> dict[str, Any]:
    return {
        "org": world.acme.id, "task": world.acme.ids["tasks"], "repo": "acme/cart-service", "status": "creating",
        "provider": None, "sha": None, "ttl": "1 hour", "destroyed": None, **overrides,
    }  # fmt: skip


def test_one_sandbox_per_task_and_repository(world: World) -> None:
    with tx(world.url, "app_user", world.acme.id) as conn:
        conn.execute(INSERT, row(world))
        conn.execute(INSERT, row(world, repo="acme/web-app"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(INSERT, row(world))


def test_a_claim_on_conflict_does_nothing(world: World) -> None:
    claim = INSERT + " on conflict (task_id, repository) do nothing returning id"
    with tx(world.url, "app_user", world.acme.id) as conn:
        assert conn.execute(claim, row(world)).fetchone() is not None
        assert conn.execute(claim, row(world)).fetchone() is None  # someone else creates it


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "running"},
        {"repo": "not a repo"},
        {"repo": "../etc/passwd"},
        {"sha": "not-a-sha"},
        {"status": "ready"},  # ready without provider id and commit
        {"status": "ready", "provider": "sbx-1"},
        {"status": "destroyed"},  # destroyed without a time
        {"destroyed": "2026-01-01T00:00:00Z"},  # a time without being destroyed
    ],
)
def test_invalid_sandboxes_are_refused(world: World, overrides: dict[str, Any]) -> None:
    with tx(world.url, "app_user", world.acme.id) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(INSERT, row(world, **overrides))


def test_a_ready_sandbox_names_its_provider_and_commit(world: World) -> None:
    with tx(world.url, "app_user", world.acme.id) as conn:
        conn.execute(INSERT, row(world, status="ready", provider="sbx-1", sha="0123abcd"))


def test_only_maintenance_lists_expired_sandboxes_of_every_org(world: World) -> None:
    for role in ("app_user", "app_vault", "app_identity_sync"):
        with tx(world.url, role, world.acme.id) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select * from app.maintenance_expired_sandboxes()")
        with tx(world.url, role, world.acme.id) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select app.maintenance_sandbox_destroyed(gen_random_uuid())")
    with tx(world.url) as conn:
        conn.execute(INSERT, row(world, repo="acme/old", ttl="-1 minute"))
        conn.execute(INSERT, row(world, org=world.globex.id, task=world.globex.ids["tasks"], repo="globex/old",
                                 ttl="-2 minutes", status="ready", provider="sbx-g", sha="abcdef0"))  # fmt: skip
        conn.execute(INSERT, row(world, repo="acme/fresh"))
        conn.execute("set local role app_maintenance")
        expired = conn.execute(
            "select org_id, provider_id from app.maintenance_expired_sandboxes()"
        ).fetchall()
        assert expired == [(world.globex.id, "sbx-g"), (world.acme.id, None)]  # oldest first
        sid = conn.execute("select id from app.maintenance_expired_sandboxes(1)").fetchone()
        assert sid is not None
        conn.execute("select app.maintenance_sandbox_destroyed(%s)", (sid[0],))
        conn.execute("select app.maintenance_sandbox_destroyed(%s)", (sid[0],))  # idempotent
        conn.execute("reset role")
        assert scalar(conn, "select status from app.sandboxes where id = %s", (sid[0],)) == "destroyed"
        assert scalar(conn, "select destroyed_at is not null from app.sandboxes where id = %s", (sid[0],))

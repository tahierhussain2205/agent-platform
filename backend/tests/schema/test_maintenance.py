"""The maintenance job runner (`app maintenance`)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import sql

from app import cli, maintenance
from app.db import roles
from app.db.roles import LoginUser, ensure_login_user
from tests.conftest import ADMIN_URL
from tests.schema.test_db_roles import PASSWORD, _url_for
from tests.world import World


@pytest.fixture
def maintenance_user(world: World, monkeypatch: pytest.MonkeyPatch) -> Iterator[LoginUser]:
    user = LoginUser("maintenance", f"t{uuid.uuid4().hex[:6]}_maintenance", "app_maintenance")
    with psycopg.connect(world.url, autocommit=True) as conn:
        ensure_login_user(conn, user, PASSWORD)
    monkeypatch.setitem(roles.LOGIN_USERS, "maintenance", user)
    yield user
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(user.username)))


def _dsn(world: World, user: LoginUser) -> str:
    return _url_for(user.username, PASSWORD, world.url.rsplit("/", 1)[1])


def _next_month_partition() -> str:
    now = datetime.now(UTC)
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return f"audit_log_{year:04d}_{month:02d}"


def test_partitions_job_recreates_a_missing_future_partition(
    world: World, maintenance_user: LoginUser
) -> None:
    name = _next_month_partition()
    with psycopg.connect(world.url) as conn:
        conn.execute(sql.SQL("drop table app.{}").format(sql.Identifier(name)))
        conn.commit()
    (result,) = maintenance.run_all(_dsn(world, maintenance_user))
    assert result.ok
    assert result.detail.startswith("created 1 partition(s)")
    with psycopg.connect(world.url) as conn:
        assert conn.execute("select to_regclass(%s)", (f"app.{name}",)).fetchone() == (f"app.{name}",)
    (again,) = maintenance.run_all(_dsn(world, maintenance_user))
    assert again.detail.startswith("created 0 partition(s)")


def test_a_failing_job_does_not_stop_the_others(world: World, maintenance_user: LoginUser) -> None:
    def boom(conn: psycopg.Connection) -> str:
        raise RuntimeError("job exploded")

    jobs = [("boom", boom), ("partitions", maintenance.ensure_partitions)]
    results = maintenance.run_all(_dsn(world, maintenance_user), jobs)
    assert [(r.name, r.ok) for r in results] == [("boom", False), ("partitions", True)]


def test_cli_runs_jobs_as_the_maintenance_user(
    world: World,
    maintenance_user: LoginUser,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", world.url)
    monkeypatch.setenv("DB_MAINTENANCE_PASSWORD", PASSWORD)
    assert cli.main(["maintenance"]) == 0
    assert "partitions: ok" in capsys.readouterr().out


def test_cli_exit_code_reflects_failures(
    world: World,
    maintenance_user: LoginUser,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken(conn: psycopg.Connection) -> str:
        raise RuntimeError("nope")

    monkeypatch.setattr(maintenance, "JOBS", [("broken", broken)])
    monkeypatch.setenv("DATABASE_URL", world.url)
    monkeypatch.setenv("DB_MAINTENANCE_PASSWORD", PASSWORD)
    assert cli.main(["maintenance"]) == 1
    assert "broken: FAILED (RuntimeError)" in capsys.readouterr().out


def test_cli_without_credentials_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_MAINTENANCE_PASSWORD", PASSWORD)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        cli.main(["maintenance"])

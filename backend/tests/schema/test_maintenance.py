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


def test_active_workspaces_are_listed_for_maintenance_only(world: World) -> None:
    """Cross tenant listing without BYPASSRLS: a security definer function only `app_maintenance`
    may run, returning active workspaces only."""
    with psycopg.connect(world.url) as conn:
        conn.execute(
            "update app.workspaces set status = 'uninstalled' where id = %s", (world.globex.workspace,)
        )
        conn.execute("set local role app_maintenance")
        listed = conn.execute(
            "select org_id, workspace_id, platform from app.maintenance_active_workspaces()"
        ).fetchall()
        conn.rollback()
    assert (world.acme.id, world.acme.workspace, "slack") in listed
    assert all(w != world.globex.workspace for _, w, _ in listed)
    for role in ("app_user", "app_vault", "app_identity_sync"):
        with psycopg.connect(world.url) as conn:
            conn.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("select * from app.maintenance_active_workspaces()")


def test_slack_reconcile_job_starts_one_sync_per_workspace_per_hour(
    world: World, maintenance_user: LoginUser
) -> None:
    import asyncio

    from temporalio.client import WorkflowExecutionStatus

    from app.settings import Settings
    from app.temporal import connect_temporal
    from tests.test_worker import TEMPORAL, _reachable

    if not _reachable(TEMPORAL):
        pytest.skip(f"Temporal not reachable at {TEMPORAL} (run `make infra`)")
    # A queue no worker polls: the workflows stay pending and are terminated below.
    settings = Settings(temporal_address=TEMPORAL, temporal_task_queue=f"test-{uuid.uuid4().hex[:8]}")
    job = maintenance.slack_reconcile_job(settings)
    with psycopg.connect(world.url) as conn:
        active = conn.execute(
            "select id from app.workspaces where platform = 'slack' and status = 'active'"
        ).fetchall()
    (first,) = maintenance.run_all(_dsn(world, maintenance_user), [job])
    (second,) = maintenance.run_all(_dsn(world, maintenance_user), [job])
    assert first.ok, first.detail
    assert second.ok, second.detail
    assert first.detail == f"{len(active)} reconciliation(s) started for {len(active)} workspace(s)"
    assert second.detail.startswith("0 reconciliation(s) started")  # same hour: deduplicated

    async def check_and_clean() -> None:
        client = await connect_temporal(settings)
        hour = datetime.now(UTC).strftime("%Y%m%d%H")
        assert {world.acme.workspace, world.globex.workspace} <= {w for (w,) in active}
        for (workspace,) in active:
            handle = client.get_workflow_handle(f"slack-reconcile-{workspace}-{hour}")
            description = await handle.describe()
            assert description.workflow_type == "SyncWorkspaceWorkflow"
            assert description.status == WorkflowExecutionStatus.RUNNING
            await handle.terminate("test cleanup")

    asyncio.run(check_and_clean())

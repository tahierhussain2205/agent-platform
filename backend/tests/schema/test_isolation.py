"""Tenant isolation, generically, for every table with an org_id column (spec 13.1, 13.4)."""

import uuid
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from tests.schema.conftest import TENANT_TABLES, World, scalar, tx

APP_USER = "app_user"
# app_user has no privileges at all on token ciphertext; only app_vault does.
NO_ACCESS_FOR_APP_USER = {"connection_secrets"}


def _row_json(world: World, table: str, org: uuid.UUID) -> dict[str, Any]:
    """One row of `org` in `table` as JSON (read as superuser), with a fresh id if it has one."""
    with tx(world.url) as conn:
        row = scalar(
            conn,
            sql.SQL("select to_jsonb(t) from app.{} t where org_id = %s limit 1").format(
                sql.Identifier(table)
            ),
            (org,),
        )
    data: dict[str, Any] = dict(row)
    if "id" in data:
        data["id"] = str(uuid.uuid4())
    return data


def _insert_json(conn: psycopg.Connection[Any], table: str, data: dict[str, Any]) -> None:
    ident = sql.Identifier(table)
    conn.execute(
        sql.SQL("insert into app.{} select * from jsonb_populate_record(null::app.{}, %s)").format(
            ident, ident
        ),
        (Jsonb(data),),
    )


def test_every_tenant_table_is_covered(world: World) -> None:
    with tx(world.url) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                """
                select c.relname from pg_class c
                  join pg_namespace n on n.oid = c.relnamespace
                  join pg_attribute a on a.attrelid = c.oid and a.attname = 'org_id' and not a.attisdropped
                 where n.nspname = 'app' and c.relkind in ('r', 'p') and not c.relispartition
                """
            )
        }
    assert tables == set(TENANT_TABLES), "add new tenant tables to TENANT_TABLES (and its isolation tests)"


def test_rls_enabled_on_every_app_table(world: World) -> None:
    with tx(world.url) as conn:
        missing = conn.execute(
            """
            select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
             where n.nspname = 'app' and c.relkind in ('r', 'p') and not c.relispartition
               and not c.relrowsecurity
            """
        ).fetchall()
    assert missing == []


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_reads_see_only_the_current_org(world: World, table: str) -> None:
    query = sql.SQL(
        "select count(*), count(*) filter (where org_id is distinct from %s and org_id is not null) from app.{}"
    ).format(sql.Identifier(table))
    with tx(world.url, APP_USER, world.acme.id) as conn:
        if table in NO_ACCESS_FOR_APP_USER:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(query, (world.acme.id,))
            return
        row = conn.execute(query, (world.acme.id,)).fetchone()
    assert row is not None
    total, foreign = row
    assert total > 0, f"world fixture has no {table} rows for acme"
    assert foreign == 0


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_no_context_means_no_rows(world: World, table: str) -> None:
    query = sql.SQL("select count(*) from app.{} where org_id is not null").format(sql.Identifier(table))
    with tx(world.url, APP_USER) as conn:
        if table in NO_ACCESS_FOR_APP_USER:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(query)
            return
        assert scalar(conn, query) == 0


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_cannot_insert_a_row_for_another_org(world: World, table: str) -> None:
    data = _row_json(world, table, world.globex.id)
    with tx(world.url, APP_USER, world.acme.id) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        _insert_json(conn, table, data)


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_cannot_write_without_context(world: World, table: str) -> None:
    data = _row_json(world, table, world.acme.id)
    with tx(world.url, APP_USER) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        _insert_json(conn, table, data)


@pytest.mark.parametrize("table", TENANT_TABLES)
@pytest.mark.parametrize(
    "statement",
    ["update app.{} set org_id = org_id where org_id = %s", "delete from app.{} where org_id = %s"],
)
def test_updates_and_deletes_cannot_reach_another_org(world: World, table: str, statement: str) -> None:
    query = sql.SQL(statement).format(sql.Identifier(table))
    with tx(world.url, APP_USER, world.acme.id) as conn:
        try:
            cur = conn.execute(query, (world.globex.id,))
        except psycopg.errors.InsufficientPrivilege:
            return  # no such privilege at all: also isolated
        assert cur.rowcount == 0
    with tx(world.url) as conn:
        assert (
            scalar(
                conn,
                sql.SQL("select count(*) from app.{} where org_id = %s").format(sql.Identifier(table)),
                (world.globex.id,),
            )
            > 0
        )


@pytest.mark.parametrize("table", TENANT_TABLES)
def test_cannot_move_a_row_to_another_org(world: World, table: str) -> None:
    ident = sql.Identifier(table)
    query = sql.SQL(
        "update app.{} set org_id = %s where ctid = (select ctid from app.{} where org_id = %s limit 1)"
    ).format(ident, ident)
    with tx(world.url, APP_USER, world.acme.id) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(query, (world.globex.id, world.acme.id))


def test_orgs_table_shows_only_the_current_org(world: World) -> None:
    with tx(world.url, APP_USER, world.acme.id) as conn:
        assert conn.execute("select id from app.orgs").fetchall() == [(world.acme.id,)]
        assert (
            conn.execute("update app.orgs set name = 'Hacked' where id = %s", (world.globex.id,)).rowcount
            == 0
        )
    with tx(world.url, APP_USER) as conn:
        assert scalar(conn, "select count(*) from app.orgs") == 0


# ------------------------------------------------------------------ composite foreign keys


CROSS_ORG_LINKS: list[tuple[str, str]] = [
    ("channel_members", "insert into app.channel_members (org_id, channel_id, external_user_id) values (%(a)s, %(b_channel)s, 'U1')"),
    ("membership_roles", "insert into app.membership_roles (org_id, membership_id, role_id, is_primary) values (%(a)s, %(a_user)s, %(b_role)s, false)"),
    ("sessions", "insert into app.sessions (org_id, workspace_id, external_channel_id, external_thread_id) values (%(a)s, %(b_workspace)s, 'C', 'T')"),
    ("tasks", "insert into app.tasks (org_id, session_id, trigger, model_tier) values (%(a)s, %(b_session)s, 'mention', 'fast')"),
    ("approvals", "insert into app.approvals (org_id, task_id, action_summary, action_tier, expires_at) values (%(a)s, %(b_task)s, 'x', 'irreversible', now() + interval '1 hour')"),
    ("schedules", "insert into app.schedules (org_id, owner_membership_id, name, instruction, cron, delivery_type, delivery_channel_id) values (%(a)s, %(a_user)s, 'n', 'i', '0 9 * * *', 'channel', %(b_channel)s)"),
    ("service_owners", "insert into app.service_owners (org_id, service_id, membership_id) values (%(a)s, %(a_service)s, %(b_user)s)"),
    ("connections", "insert into app.connections (org_id, integration_key, scope, membership_id) values (%(a)s, 'github', 'user', %(b_user)s)"),
    ("external_actions", "insert into app.external_actions (org_id, task_id, integration_key, operation, idempotency_key) values (%(a)s, %(b_task)s, 'jira', 'op', 'cross-org-key')"),
    ("artifacts", "insert into app.artifacts (org_id, task_id, kind, content) values (%(a)s, %(b_task)s, 'brief', '{}')"),
    ("autonomy_policies", "insert into app.autonomy_policies (org_id, scope_type, repository_id, max_mode) values (%(a)s, 'repo', %(b_repo)s, 'auto')"),
]  # fmt: skip


@pytest.mark.parametrize(("table", "statement"), CROSS_ORG_LINKS, ids=[t for t, _ in CROSS_ORG_LINKS])
def test_cannot_link_to_another_orgs_row_even_with_its_id(world: World, table: str, statement: str) -> None:
    a, b = world.acme, world.globex
    with tx(world.url) as conn:
        b_role = scalar(conn, "select id from app.custom_roles where org_id = %s limit 1", (b.id,))
    params = {
        "a": a.id, "a_user": a.user, "a_service": a.ids["services"],
        "b_channel": b.channel, "b_role": b_role, "b_workspace": b.workspace, "b_session": b.ids["sessions"],
        "b_task": b.ids["tasks"], "b_user": b.user, "b_repo": b.ids["repositories"],
    }  # fmt: skip
    with tx(world.url, APP_USER, a.id) as conn, pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(statement, params)

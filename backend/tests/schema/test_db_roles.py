"""Login users per component, SCRAM verifiers, component DSNs, and migrating as a Render-like user."""

import re
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg import sql
from pydantic import SecretStr

from app.db import roles
from app.db.migrate import discover, ensure_login_users, migrate
from app.db.roles import LoginUser, ensure_login_user, scram_sha256_verifier
from app.settings import Settings
from tests.conftest import ADMIN_URL, database_url
from tests.world import World

PASSWORD = "a-very-long-test-password-1"


def _url_for(user: str, password: str, dbname: str) -> str:
    base = database_url(dbname)
    return re.sub(r"//[^@]*@", f"//{user}:{password}@", base)


def _dbname(url: str) -> str:
    return url.rsplit("/", 1)[1]


@pytest.fixture
def login_users(world: World) -> Iterator[dict[str, LoginUser]]:
    """Login users with unique names (roles are cluster wide; never touch the real names)."""
    tag = uuid.uuid4().hex[:6]
    users: dict[str, LoginUser] = {
        c: LoginUser(c, f"t{tag}_{u.username}", u.member_of) for c, u in roles.LOGIN_USERS.items()
    }
    with psycopg.connect(world.url, autocommit=True) as conn:
        for user in users.values():
            ensure_login_user(conn, user, PASSWORD)
    yield users
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        for user in users.values():
            conn.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(user.username)))


def _connect(world: World, user: LoginUser, password: str = PASSWORD) -> psycopg.Connection[Any]:
    return psycopg.connect(_url_for(user.username, password, _dbname(world.url)))


# ------------------------------------------------------------------ SCRAM


def test_scram_verifier_format_and_determinism() -> None:
    v = scram_sha256_verifier("secret", salt=b"0123456789abcdef")
    assert re.fullmatch(r"SCRAM-SHA-256\$4096:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+", v)
    assert v == scram_sha256_verifier("secret", salt=b"0123456789abcdef")
    assert v != scram_sha256_verifier("secret")  # random salt by default
    assert "secret" not in v


def test_short_passwords_are_refused(world: World) -> None:
    with psycopg.connect(world.url, autocommit=True) as conn, pytest.raises(ValueError, match="at least 16"):
        ensure_login_user(conn, LoginUser("api", "never_created", "app_user"), "short")


# ------------------------------------------------------------------ login users


def test_login_users_connect_and_hold_only_their_role(
    world: World, login_users: dict[str, LoginUser]
) -> None:
    with psycopg.connect(world.url) as conn:
        for user in login_users.values():
            row = conn.execute(
                "select rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, "
                "array(select g.rolname::text from pg_auth_members m join pg_roles g on g.oid = m.roleid "
                "where m.member = r.oid) from pg_roles r where rolname = %s",
                (user.username,),
            ).fetchone()
            assert row == (False, False, False, False, [user.member_of])
    for user in login_users.values():
        with _connect(world, user) as conn:
            assert conn.execute("select current_user").fetchone() == (user.username,)


def test_wrong_password_is_rejected(world: World, login_users: dict[str, LoginUser]) -> None:
    with pytest.raises(psycopg.OperationalError, match="password authentication failed"):
        _connect(world, login_users["api"], "not-the-password")


def test_password_rotation(world: World, login_users: dict[str, LoginUser]) -> None:
    new = "rotated-password-0000000002"
    with psycopg.connect(world.url, autocommit=True) as conn:
        assert ensure_login_user(conn, login_users["api"], new) is False  # existed: updated
    with pytest.raises(psycopg.OperationalError):
        _connect(world, login_users["api"])
    with _connect(world, login_users["api"], new) as conn:
        assert conn.execute("select 1").fetchone() == (1,)


def test_unchanged_password_keeps_the_verifier(world: World, login_users: dict[str, LoginUser]) -> None:
    """A new salt on every migration would break PgBouncer's SCRAM pass-through for services that
    authenticated before it (they cannot open new server connections until they reconnect)."""

    def verifier(conn: psycopg.Connection) -> str:
        row = conn.execute(
            "select rolpassword from pg_authid where rolname = %s", (login_users["api"].username,)
        ).fetchone()
        assert row is not None
        return str(row[0])

    with psycopg.connect(world.url, autocommit=True) as conn:
        before = verifier(conn)
        assert ensure_login_user(conn, login_users["api"], PASSWORD) is False
        assert verifier(conn) == before
        ensure_login_user(conn, login_users["api"], "rotated-password-0000000003")
        assert verifier(conn) != before


def test_extra_privileges_fail_closed(world: World, login_users: dict[str, LoginUser]) -> None:
    api = login_users["api"]
    with psycopg.connect(world.url, autocommit=True) as conn:
        conn.execute(sql.SQL("grant app_vault to {}").format(sql.Identifier(api.username)))
        with pytest.raises(RuntimeError, match="unexpected attributes or memberships"):
            ensure_login_user(conn, api, PASSWORD)


def test_api_user_is_bound_by_rls_and_grants(world: World, login_users: dict[str, LoginUser]) -> None:
    with _connect(world, login_users["api"]) as conn:
        assert conn.execute("select count(*) from app.channels").fetchone() == (0,)  # no context
        conn.execute("select set_config('app.org_id', %s, true)", (str(world.acme.id),))
        orgs = {r[0] for r in conn.execute("select distinct org_id from app.channels")}
        assert orgs == {world.acme.id}
        for statement in (
            "select * from app.connection_secrets",
            "select app.ensure_monthly_partitions(1)",
            "set role app_vault",
            "set role app_maintenance",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()


def test_vault_user_reads_secrets_for_current_org_only(
    world: World, login_users: dict[str, LoginUser]
) -> None:
    with _connect(world, login_users["vault"]) as conn:
        conn.execute("select set_config('app.org_id', %s, true)", (str(world.globex.id),))
        assert conn.execute("select ciphertext from app.connection_secrets").fetchall() == [
            (b"secret-of-globex",)
        ]


def test_identity_user_cannot_read_work_data(world: World, login_users: dict[str, LoginUser]) -> None:
    with (
        _connect(world, login_users["identity"]) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute("select * from app.tasks")


def test_maintenance_user_can_manage_partitions(world: World, login_users: dict[str, LoginUser]) -> None:
    with _connect(world, login_users["maintenance"]) as conn:
        assert conn.execute("select app.ensure_monthly_partitions(3)").fetchone() == (0,)


def test_ensure_login_users_creates_only_configured_components(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    tag = uuid.uuid4().hex[:6]
    fake = {c: LoginUser(c, f"t{tag}_{u.username}", u.member_of) for c, u in roles.LOGIN_USERS.items()}
    monkeypatch.setattr("app.db.migrate.LOGIN_USERS", fake)
    settings = Settings(db_api_password=SecretStr(PASSWORD), db_maintenance_password=SecretStr(PASSWORD))
    try:
        created = ensure_login_users(world.url, settings)
        assert created == [fake["api"].username, fake["maintenance"].username]
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            for user in fake.values():
                conn.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(user.username)))


# ------------------------------------------------------------------ component DSN


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        (
            "postgresql://render:pw@db.internal:5432/agent",
            "postgresql://api_service:p%40ss%3Aw%2Ford@db.internal:5432/agent",
        ),
        (
            "postgresql://render:pw@db.internal/agent?sslmode=require",
            "postgresql://api_service:p%40ss%3Aw%2Ford@db.internal/agent?sslmode=require",
        ),
        (
            "postgresql://render:pw@[::1]:6432/agent",
            "postgresql://api_service:p%40ss%3Aw%2Ford@[::1]:6432/agent",
        ),
    ],
)
def test_component_dsn_swaps_credentials(base: str, expected: str) -> None:
    s = Settings(database_url=SecretStr(base), db_api_password=SecretStr("p@ss:w/ord"))
    assert s.component_dsn("api") == expected


def test_component_dsn_requires_configuration() -> None:
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        Settings(db_api_password=SecretStr(PASSWORD)).component_dsn("api")
    with pytest.raises(RuntimeError, match="DB_WORKER_PASSWORD"):
        Settings(database_url=SecretStr("postgresql://u:p@h/d")).component_dsn("worker")


# ------------------------------------------------------------------ Render-like migrator


@pytest.fixture
def render_like_db(postgres: None) -> Iterator[str]:
    """A database owned by a non superuser with CREATEROLE: the assumed Render default user.

    It is given ADMIN (only) on the app roles, which is what it would hold had it created them.
    """
    tag = uuid.uuid4().hex[:6]
    user, dbname = f"render_like_{tag}", f"render_like_{tag}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(
            sql.SQL("create role {} login createrole createdb password {}").format(
                sql.Identifier(user), sql.Literal(PASSWORD)
            )
        )
        for role in ("app_owner", "app_user", "app_vault", "app_identity_sync", "app_maintenance"):
            conn.execute(
                f"do $$ begin if not exists (select 1 from pg_roles where rolname = '{role}') then create role {role} nologin; end if; end $$"
            )
            conn.execute(
                sql.SQL("grant {} to {} with admin true, inherit false, set false").format(
                    sql.Identifier(role), sql.Identifier(user)
                )
            )
        conn.execute(
            sql.SQL("create database {} owner {}").format(sql.Identifier(dbname), sql.Identifier(user))
        )
    try:
        yield _url_for(user, PASSWORD, dbname)
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            conn.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(dbname)))
            conn.execute(sql.SQL("drop owned by {}").format(sql.Identifier(user)))
            conn.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(user)))


def test_migrations_apply_as_a_render_like_user(
    render_like_db: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="app.db.migrate")
    with psycopg.connect(render_like_db) as conn:
        flags = conn.execute(
            "select rolsuper, rolbypassrls, rolcreaterole from pg_roles where rolname = current_user"
        ).fetchone()
    assert flags == (False, False, True)
    assert migrate(render_like_db) == [m.version for m in discover()]
    assert "skipped, needs a superuser: alter role app_identity_sync bypassrls" in caplog.text
    assert migrate(render_like_db) == []
    with psycopg.connect(render_like_db) as conn:
        assert conn.execute(
            "select nspowner::regrole::text from pg_namespace where nspname = 'app'"
        ).fetchone() == ("app_owner",)
        assert conn.execute("select tableowner from pg_tables where tablename = 'channels'").fetchone() == (
            "app_owner",
        )
        assert conn.execute(
            "select count(*) from pg_roles where rolname in ('app_user', 'app_vault') and rolbypassrls"
        ).fetchone() == (0,)
        # The migrator can work as app_owner in later migrations.
        conn.execute("set role app_owner")


def test_render_like_user_can_create_login_users(render_like_db: str) -> None:
    migrate(render_like_db)
    user = LoginUser("api", f"t{uuid.uuid4().hex[:6]}_api", "app_user")
    try:
        with psycopg.connect(render_like_db, autocommit=True) as conn:
            assert ensure_login_user(conn, user, PASSWORD) is True
            assert ensure_login_user(conn, user, PASSWORD) is False  # rotation path as non superuser
        with psycopg.connect(_url_for(user.username, PASSWORD, _dbname(render_like_db))) as conn:
            assert conn.execute("select count(*) from app.channels").fetchone() == (0,)
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            conn.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(user.username)))

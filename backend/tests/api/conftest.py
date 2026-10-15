"""Fixtures for API tests: a login user for the API, pools (direct and through PgBouncer), and a
test app whose routes exercise the unit of work."""

import os
import socket
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest
import pytest_asyncio
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Header, HTTPException
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from app.api.main import Resources, create_app
from app.db.pool import open_pool
from app.db.roles import LoginUser, ensure_login_user
from app.db.uow import UnitOfWork, get_uow
from app.settings import Settings
from tests.conftest import ADMIN_URL
from tests.world import World

PGBOUNCER = os.environ.get("TEST_PGBOUNCER_HOSTPORT", "localhost:56432")
PASSWORD = "api-test-password-000000001"


def _reachable(hostport: str) -> bool:
    host, _, port = hostport.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def api_login(world: World) -> Iterator[str]:
    user = LoginUser("api", f"t{uuid.uuid4().hex[:6]}_api", "app_user")
    with psycopg.connect(world.url, autocommit=True) as conn:
        ensure_login_user(conn, user, PASSWORD)
    yield user.username
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(user.username)))


def _dsn(world: World, user: str, hostport: str | None = None) -> str:
    base, _, dbname = world.url.rpartition("/")
    host = base.rsplit("@", 1)[1]
    return f"postgresql://{user}:{PASSWORD}@{hostport or host}/{dbname}"


@pytest.fixture(scope="session", params=["direct", "pgbouncer"])
def api_dsn(request: pytest.FixtureRequest, world: World, api_login: str) -> str:
    if request.param == "pgbouncer":
        if not _reachable(PGBOUNCER):
            pytest.skip(f"PgBouncer not reachable at {PGBOUNCER} (run `make infra`)")
        return _dsn(world, api_login, PGBOUNCER)
    return _dsn(world, api_login)


def uow_router(world: World) -> APIRouter:
    """Routes that exercise the unit of work. Org comes from a header (auth arrives in phase 5)."""
    router = APIRouter(prefix="/t")
    acme, globex = world.acme, world.globex

    @router.get("/channels")
    async def channels(x_org: UUID = Header(), uow: UnitOfWork = Depends(get_uow)) -> dict[str, Any]:
        conn = await uow.bind(x_org)
        rows = await (await conn.execute("select org_id, name from app.channels")).fetchall()
        setting = await (await conn.execute("select current_setting('app.org_id', true)")).fetchone()
        return {"orgs": sorted({str(r[0]) for r in rows}), "setting": setting[0] if setting else None}

    @router.post("/write")
    async def write(
        marker: str, x_org: UUID = Header(), uow: UnitOfWork = Depends(get_uow)
    ) -> dict[str, str]:
        conn = await uow.bind(x_org)
        await conn.execute(
            "insert into app.audit_log (org_id, actor_type, action, reason) values (%s, 'system', 'test', %s)",
            (x_org, marker),
        )
        return {"ok": marker}

    @router.post("/write-then-fail")
    async def write_then_fail(marker: str, uow: UnitOfWork = Depends(get_uow)) -> None:
        conn = await uow.bind(acme.id)
        await conn.execute(
            "insert into app.audit_log (org_id, actor_type, action, reason) values (%s, 'system', 'test', %s)",
            (acme.id, marker),
        )
        raise HTTPException(400, "nope")

    @router.post("/write-then-crash")
    async def write_then_crash(marker: str, uow: UnitOfWork = Depends(get_uow)) -> None:
        conn = await uow.bind(acme.id)
        await conn.execute(
            "insert into app.audit_log (org_id, actor_type, action, reason) values (%s, 'system', 'test', %s)",
            (acme.id, marker),
        )
        raise RuntimeError("boom with secret xoxb-123456789012-abcdefghij")

    @router.post("/roles-without-primary")
    async def roles_without_primary(marker: str, uow: UnitOfWork = Depends(get_uow)) -> dict[str, str]:
        conn = await uow.bind(acme.id)
        await conn.execute(
            "insert into app.audit_log (org_id, actor_type, action, reason) values (%s, 'system', 'test', %s)",
            (acme.id, marker),
        )
        quinn = await (await conn.execute(
            "select m.id from app.org_memberships m join app.users u on u.id = m.user_id where u.email = 'quinn@acme.test'"
        )).fetchone()  # fmt: skip
        assert quinn is not None
        await conn.execute(
            "update app.membership_roles set is_primary = false where membership_id = %s", (quinn[0],)
        )
        return {"ok": "handler finished"}  # the deferred primary-role check fails at commit

    @router.get("/two-orgs")
    async def two_orgs(uow: UnitOfWork = Depends(get_uow)) -> None:
        await uow.bind(acme.id)
        await uow.bind(globex.id)

    @router.get("/same-org-twice")
    async def same_org_twice(uow: UnitOfWork = Depends(get_uow)) -> dict[str, bool]:
        a = await uow.bind(acme.id)
        b = await uow.bind(acme.id)
        return {"same": a is b}

    @router.get("/no-bind")
    async def no_bind(uow: UnitOfWork = Depends(get_uow)) -> dict[str, bool]:
        return {"bound": uow.bound}

    @router.get("/background")
    async def background(tasks: BackgroundTasks, uow: UnitOfWork = Depends(get_uow)) -> dict[str, str]:
        await uow.bind(acme.id)

        async def later() -> None:
            try:
                await uow.connection.execute("select 1")
                BACKGROUND_RESULT.append("used")
            except Exception as exc:
                BACKGROUND_RESULT.append(type(exc).__name__)

        tasks.add_task(later)
        return {"ok": "scheduled"}

    errors = {
        "unique": "insert into app.custom_roles (org_id, title) values (%(a)s, 'Engineer')",
        "check": "insert into app.schedules (org_id, owner_membership_id, name, instruction, cron, timezone, delivery_type) values (%(a)s, %(u)s, 'n', 'i', '0 9 * * *', 'Nowhere/Land', 'dm')",
        "fk": "insert into app.channel_members (org_id, channel_id, external_user_id) values (%(a)s, %(missing)s, 'U1')",
        "rls": "insert into app.custom_roles (org_id, title) values (%(b)s, 'Sneaky')",
        "grant": "update app.org_memberships set privilege = 'admin'",
        "notnull": "insert into app.custom_roles (org_id, title) values (%(a)s, null)",
    }  # fmt: skip

    @router.post("/db-error/{kind}")
    async def db_error(kind: str, uow: UnitOfWork = Depends(get_uow)) -> None:
        conn = await uow.bind(acme.id)
        await conn.execute(
            errors[kind], {"a": acme.id, "b": globex.id, "u": acme.user, "missing": uuid.uuid4()}
        )

    return router


BACKGROUND_RESULT: list[str] = []


def build_app(world: World, pool: AsyncConnectionPool | None, kv: Any = None) -> FastAPI:
    app = create_app(Settings(env="test"), Resources(db_pool=pool, kv=kv))
    app.include_router(uow_router(world))
    return app


@pytest_asyncio.fixture(loop_scope="session")
async def pool(api_dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    p = await open_pool(api_dsn, "test", min_size=0, max_size=4)
    yield p
    await p.close()


@pytest_asyncio.fixture(loop_scope="session")
async def client(world: World, pool: AsyncConnectionPool) -> AsyncIterator[httpx.AsyncClient]:
    app = build_app(world, pool)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as c:
        yield c


def marker_count(world: World, marker: str) -> int:
    with psycopg.connect(world.url) as conn:
        row = conn.execute("select count(*) from app.audit_log where reason = %s", (marker,)).fetchone()
        assert row is not None
        return int(row[0])

"""Unit of work middleware (spec 14.1): one org per request, commit before the response starts,
rollback on errors, no context leaking across pooled connections. Every test runs twice: direct
to Postgres and through PgBouncer in transaction mode."""

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from psycopg_pool import AsyncConnectionPool
from starlette.types import Message, Receive, Scope, Send

from tests.api.conftest import BACKGROUND_RESULT, build_app, marker_count
from tests.world import World


def org(world: World, which: str) -> dict[str, str]:
    return {"x-org": str(getattr(world, which).id)}


async def test_reads_are_scoped_to_the_bound_org(client: httpx.AsyncClient, world: World) -> None:
    for which in ("acme", "globex"):
        body = (await client.get("/t/channels", headers=org(world, which))).json()
        assert body == {"orgs": [str(getattr(world, which).id)], "setting": str(getattr(world, which).id)}


async def test_successful_write_is_committed(client: httpx.AsyncClient, world: World) -> None:
    marker = uuid.uuid4().hex
    response = await client.post("/t/write", params={"marker": marker}, headers=org(world, "acme"))
    assert response.status_code == 200
    assert marker_count(world, marker) == 1


async def test_commit_happens_before_the_response_starts(world: World, pool: AsyncConnectionPool) -> None:
    """When the first response byte leaves, the write must already be visible to others."""
    app = build_app(world, pool)
    seen_at_start: list[int] = []
    marker = uuid.uuid4().hex

    async def observing_app(scope: Scope, receive: Receive, send: Send) -> None:
        async def observing_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                seen_at_start.append(await asyncio.to_thread(marker_count, world, marker))
            await send(message)

        await app(scope, receive, observing_send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=observing_app, raise_app_exceptions=False), base_url="http://t"
    ) as c:
        response = await c.post("/t/write", params={"marker": marker}, headers=org(world, "acme"))
    assert response.status_code == 200
    assert seen_at_start == [1]


async def test_deferred_constraint_failure_replaces_the_response(
    client: httpx.AsyncClient, world: World
) -> None:
    marker = uuid.uuid4().hex
    response = await client.post("/t/roles-without-primary", params={"marker": marker})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "invalid"
    assert "handler finished" not in response.text
    assert body["error"]["request_id"] == response.headers["x-request-id"]
    assert marker_count(world, marker) == 0


async def test_error_response_rolls_back(client: httpx.AsyncClient, world: World) -> None:
    marker = uuid.uuid4().hex
    assert (await client.post("/t/write-then-fail", params={"marker": marker})).status_code == 400
    assert marker_count(world, marker) == 0


async def test_exception_rolls_back_and_hides_details(client: httpx.AsyncClient, world: World) -> None:
    marker = uuid.uuid4().hex
    response = await client.post("/t/write-then-crash", params={"marker": marker})
    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "internal",
        "message": "Something went wrong.",
        "request_id": response.headers["x-request-id"],
    }
    assert "xoxb" not in response.text
    assert marker_count(world, marker) == 0


async def test_binding_a_second_org_fails(client: httpx.AsyncClient) -> None:
    response = await client.get("/t/two-orgs")
    assert response.status_code == 500


async def test_binding_the_same_org_twice_is_fine(client: httpx.AsyncClient) -> None:
    assert (await client.get("/t/same-org-twice")).json() == {"same": True}


async def test_requests_that_do_not_bind_take_no_connection(world: World, pool: AsyncConnectionPool) -> None:
    app = build_app(world, pool)
    before = pool.get_stats().get("requests_num", 0)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t"
    ) as c:
        assert (await c.get("/t/no-bind")).json() == {"bound": False}
    assert pool.get_stats().get("requests_num", 0) == before


async def test_background_work_cannot_use_the_request_unit_of_work(client: httpx.AsyncClient) -> None:
    BACKGROUND_RESULT.clear()
    assert (await client.get("/t/background")).status_code == 200
    for _ in range(50):
        if BACKGROUND_RESULT:
            break
        await asyncio.sleep(0.02)
    assert BACKGROUND_RESULT == ["UnitOfWorkClosedError"]


@pytest.mark.parametrize(
    ("kind", "status", "code"),
    [("unique", 409, "conflict"), ("fk", 409, "conflict"), ("check", 422, "invalid"),
     ("notnull", 422, "invalid"), ("rls", 403, "forbidden"), ("grant", 403, "forbidden")],
)  # fmt: skip
async def test_database_errors_map_to_http(
    client: httpx.AsyncClient, kind: str, status: int, code: str
) -> None:
    response = await client.post(f"/t/db-error/{kind}")
    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    # Generic message only: the raw database text (which can include values) is not returned.
    for raw in (
        "Key (",
        "violates check constraint",
        "row-level security",
        "permission denied",
        "null value",
    ):
        assert raw not in response.text


async def test_no_org_setting_survives_on_a_pooled_connection(world: World, api_dsn: str) -> None:
    """One connection, three requests: org A, raw connection, org B."""
    from app.db.pool import open_pool

    single = await open_pool(api_dsn, "single", min_size=1, max_size=1)
    try:
        app = build_app(world, single)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t"
        ) as c:
            a = (await c.get("/t/channels", headers=org(world, "acme"))).json()
            async with single.connection() as conn:
                row = await (await conn.execute("select current_setting('app.org_id', true)")).fetchone()
                visible = await (await conn.execute("select count(*) from app.channels")).fetchone()
            b = (await c.get("/t/channels", headers=org(world, "globex"))).json()
        assert a["orgs"] == [str(world.acme.id)]
        assert row is not None
        assert row[0] in (None, "")
        assert visible == (0,)
        assert b["orgs"] == [str(world.globex.id)]
    finally:
        await single.close()


async def test_concurrent_requests_from_different_orgs_stay_isolated(
    client: httpx.AsyncClient, world: World
) -> None:
    async def one(i: int) -> tuple[str, Any]:
        which = "acme" if i % 2 else "globex"
        response = await client.get("/t/channels", headers=org(world, which))
        return str(getattr(world, which).id), response.json()

    results = await asyncio.gather(*(one(i) for i in range(60)))
    for expected, body in results:
        assert body == {"orgs": [expected], "setting": expected}


async def test_pool_connections_are_returned(
    world: World, pool: AsyncConnectionPool, client: httpx.AsyncClient
) -> None:
    for _ in range(10):
        await client.post("/t/write-then-fail", params={"marker": "x"})
        await client.get("/t/channels", headers=org(world, "acme"))
    stats = pool.get_stats()
    assert stats["pool_size"] - stats["pool_available"] == 0  # nothing checked out after requests finish

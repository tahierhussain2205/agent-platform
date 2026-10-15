"""/readyz reports dependency health without leaking configuration."""

import httpx
import pytest
from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool

from app import kv
from app.db.pool import open_pool
from tests.api.conftest import build_app
from tests.world import World


async def _get(app: FastAPI) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t"
    ) as c:
        return await c.get("/readyz")


async def test_ready_when_dependencies_are_up(world: World, pool: AsyncConnectionPool) -> None:
    client = kv.make_client("redis://localhost:56379/15")
    try:
        await client.ping()
    except Exception:
        pytest.skip("Key Value not reachable")
    response = await _get(build_app(world, pool, kv=client))
    await client.aclose()
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok", "key_value": "ok"}}


async def test_not_ready_when_database_is_down(world: World) -> None:
    dead = await open_pool(
        "postgresql://nobody:secretpw@127.0.0.1:1/none", "dead", min_size=0, max_size=1, acquire_timeout=1
    )
    try:
        response = await _get(build_app(world, dead, kv=None))
    finally:
        await dead.close()
    assert response.status_code == 503
    body = response.json()
    assert body["checks"] == {"database": "unavailable", "key_value": "not_configured"}
    assert "secretpw" not in response.text
    assert "127.0.0.1" not in response.text


async def test_not_ready_when_key_value_is_down(world: World, pool: AsyncConnectionPool) -> None:
    client = kv.make_client("redis://:kvsecret@127.0.0.1:1/0")
    response = await _get(build_app(world, pool, kv=client))
    await client.aclose()
    assert response.status_code == 503
    assert response.json()["checks"]["key_value"] == "unavailable"
    assert "kvsecret" not in response.text


async def test_not_ready_when_nothing_is_configured(world: World) -> None:
    response = await _get(build_app(world, None))
    assert response.status_code == 503
    assert response.json()["checks"] == {"database": "not_configured", "key_value": "not_configured"}


async def test_health_is_independent_of_dependencies(world: World) -> None:
    app = build_app(world, None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/healthz")).json() == {"status": "ok"}

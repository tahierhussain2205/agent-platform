"""Key Value helpers against Valkey."""

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import redis.asyncio as redis

from app import kv

URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:56379/15")


@pytest_asyncio.fixture(loop_scope="session")
async def client() -> AsyncIterator[redis.Redis]:
    c = kv.make_client(URL)
    try:
        await c.ping()
    except (redis.ConnectionError, OSError):
        pytest.skip(f"Key Value not reachable at {URL} (run `make infra`)")
    yield c
    await c.aclose()


def test_tenant_keys_include_the_org() -> None:
    org = uuid.uuid4()
    assert kv.tenant_key(org, "membership", "C0BUGS") == f"ap:t:{org}:membership:C0BUGS"


@pytest.mark.parametrize("parts", [(), ("",), ("a:b",)])
def test_bad_key_parts_rejected(parts: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="key parts"):
        kv.tenant_key(uuid.uuid4(), *parts)
    with pytest.raises(ValueError, match="key parts"):
        kv.global_key(*parts)


def test_tenant_keys_need_a_uuid() -> None:
    with pytest.raises(TypeError):
        kv.tenant_key("not-a-uuid", "x")  # type: ignore[arg-type]


async def test_claim_extend_release(client: redis.Redis) -> None:
    key = kv.global_key("test", uuid.uuid4().hex)
    assert await kv.claim(client, key, 30)
    assert not await kv.claim(client, key, 30)  # second claimant loses
    assert await kv.extend(client, key, 300)
    assert 200 < await client.ttl(key) <= 300
    await kv.release(client, key)
    assert await kv.claim(client, key, 30)  # free again after release
    await kv.release(client, key)


async def test_claims_expire(client: redis.Redis) -> None:
    key = kv.global_key("test", uuid.uuid4().hex)
    assert await kv.claim(client, key, 1)
    await client.pexpire(key, 50)
    import asyncio

    await asyncio.sleep(0.1)
    assert await kv.claim(client, key, 30)
    await kv.release(client, key)


async def test_extend_missing_key_reports_false(client: redis.Redis) -> None:
    assert not await kv.extend(client, kv.global_key("test", uuid.uuid4().hex), 30)


async def test_ping(client: redis.Redis) -> None:
    assert await kv.ping(client)

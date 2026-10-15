"""Key Value (Redis compatible) client and helpers.

Every key that holds tenant data is built with ``tenant_key`` so the org id is part of it
(spec 12.5). ``claim``/``extend``/``release`` implement the short lived claims used for
deduplication and locks.
"""

from uuid import UUID

import redis.asyncio as redis

KEY_PREFIX = "ap"  # agent platform; keeps keys recognizable in a shared instance


def make_client(url: str) -> "redis.Redis":
    return redis.from_url(url, socket_timeout=2.0, socket_connect_timeout=2.0, health_check_interval=30)


def tenant_key(org_id: UUID, *parts: str) -> str:
    if not isinstance(org_id, UUID):
        raise TypeError("tenant keys need the org id as a UUID")
    if not parts or any(not p or ":" in p for p in parts):
        raise ValueError("key parts must be non empty and contain no ':'")
    return ":".join((KEY_PREFIX, "t", str(org_id), *parts))


def global_key(*parts: str) -> str:
    """Keys that are not tenant data (for example Slack event ids before the org is known)."""
    if not parts or any(not p or ":" in p for p in parts):
        raise ValueError("key parts must be non empty and contain no ':'")
    return ":".join((KEY_PREFIX, "g", *parts))


async def claim(client: "redis.Redis", key: str, ttl_seconds: int, value: str = "1") -> bool:
    """Set the key only if absent. True if this caller now holds the claim."""
    return bool(await client.set(key, value, nx=True, ex=ttl_seconds))


async def extend(client: "redis.Redis", key: str, ttl_seconds: int) -> bool:
    return bool(await client.expire(key, ttl_seconds))


async def release(client: "redis.Redis", key: str) -> None:
    await client.delete(key)


async def ping(client: "redis.Redis") -> bool:
    return bool(await client.ping())

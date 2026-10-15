"""Helpers shared by console routes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Request
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.api.errors import ApiError
from app.auth.console import ConsoleIdentity, resources
from app.identity.clerk_api import ClerkClient


async def audit(
    conn: AsyncConnection,
    identity: ConsoleIdentity,
    action: str,
    target: str | None = None,
    metadata: dict[str, Any] | None = None,
    decision: str = "info",
) -> None:
    await conn.execute(
        "insert into app.audit_log (org_id, actor_type, actor_id, action, target, decision, metadata) "
        "values (%s, 'user', %s, %s, %s, %s, %s)",
        (identity.org_id, identity.membership_id, action, target, decision, Jsonb(metadata or {})),
    )


async def fetch_all(conn: AsyncConnection, query: str, params: Any = None) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


async def fetch_one(conn: AsyncConnection, query: str, params: Any = None) -> dict[str, Any] | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchone()


def clerk(request: Request) -> ClerkClient:
    client = resources(request).clerk_client
    if client is None:
        raise ApiError(503, "identity_unavailable", "Identity management is not available.")
    return client


@asynccontextmanager
async def identity_tx(request: Request) -> AsyncIterator[AsyncConnection]:
    """A transaction as identity_service: commits on success, rolls back if anything raises
    (including a failed Clerk call made inside it)."""
    pool = resources(request).identity_pool
    if pool is None:
        raise ApiError(503, "identity_unavailable", "Identity management is not available.")
    async with pool.connection() as conn:
        yield conn

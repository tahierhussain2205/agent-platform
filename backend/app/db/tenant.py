"""Tenant transactions outside HTTP requests (workers, ingress side work).

Same rule as the unit of work: ``app.org_id`` is set with ``set_config(..., true)`` as the first
statement of the transaction, so nothing runs without it and nothing leaks to the next user of
the pooled connection.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from app.context import org_id_var


@asynccontextmanager
async def tenant_tx(pool: AsyncConnectionPool, org_id: UUID | str) -> AsyncIterator[AsyncConnection]:
    org = org_id if isinstance(org_id, UUID) else UUID(org_id)
    token = org_id_var.set(org)
    try:
        async with pool.connection() as conn:  # commits on success, rolls back on error
            await conn.execute("select set_config('app.org_id', %s, true)", (str(org),))
            yield conn
    finally:
        org_id_var.reset(token)

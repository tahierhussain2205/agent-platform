"""Connection pools, one per component login user."""

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool


async def open_pool(
    dsn: str, name: str, min_size: int = 1, max_size: int = 10, acquire_timeout: float = 10.0
) -> AsyncConnectionPool:
    """A pool safe for PgBouncer transaction mode: no server side prepared statements (plan rule 7),
    explicit transactions, and a liveness check when a connection is handed out."""
    pool: AsyncConnectionPool[AsyncConnection] = AsyncConnectionPool(
        dsn,
        name=name,
        min_size=min_size,
        max_size=max_size,
        timeout=acquire_timeout,
        kwargs={"prepare_threshold": None, "autocommit": False},
        check=AsyncConnectionPool.check_connection,
        open=False,
    )
    await pool.open(wait=False)
    return pool

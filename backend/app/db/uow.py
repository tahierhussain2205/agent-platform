"""Unit of work per request (spec 14.1, invariants 1 and 9).

- ``bind(org_id)`` takes a pooled connection, starts the transaction and sets ``app.org_id`` with
  ``set_config(..., true)`` as its first statement. One org per request; a second org is an error.
- The middleware commits **before** the response starts. If the commit fails (deferred
  constraints), the client gets the mapped error instead of the handler's response.
- Error responses (status >= 400) and exceptions roll back.
- After the response starts the unit of work is closed: background work must not use it.
"""

import contextlib
import json
import logging
from collections.abc import Callable
from uuid import UUID

import psycopg
from fastapi import Request
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.context import org_id_var, request_id_var
from app.db.errors import MappedError, error_body, map_db_error
from app.telemetry import mark_tenant

log = logging.getLogger(__name__)


class OrgBindingError(RuntimeError):
    """A request tried to bind a second org."""


class UnitOfWorkClosedError(RuntimeError):
    """The unit of work was used after the response started."""


class UnitOfWork:
    def __init__(self, pool: AsyncConnectionPool | None) -> None:
        self._pool = pool
        self._conn: AsyncConnection | None = None
        self.org_id: UUID | None = None
        self.closed = False

    async def bind(self, org_id: UUID) -> AsyncConnection:
        if self.closed:
            raise UnitOfWorkClosedError("the unit of work is closed (the response already started)")
        if self.org_id is not None:
            if self.org_id != org_id:
                raise OrgBindingError("a request can bind only one org")
            assert self._conn is not None
            return self._conn
        if self._pool is None:
            raise RuntimeError("the database is not configured")
        conn = await self._pool.getconn()
        try:
            # First statement of the transaction. Transaction scoped (third argument true), so a
            # pooled connection never carries this org into another request.
            await conn.execute("select set_config('app.org_id', %s, true)", (str(org_id),))
        except BaseException:
            await self._pool.putconn(conn)
            raise
        self._conn, self.org_id = conn, org_id
        org_id_var.set(org_id)
        mark_tenant(org_id)
        return conn

    @property
    def connection(self) -> AsyncConnection:
        if self.closed:
            raise UnitOfWorkClosedError("the unit of work is closed (the response already started)")
        if self._conn is None:
            raise RuntimeError("the unit of work is not bound to an org")
        return self._conn

    @property
    def bound(self) -> bool:
        return self._conn is not None

    async def finish(self, commit: bool) -> None:
        """Commit or roll back, return the connection, close. Idempotent. A failed commit
        propagates (after the connection is returned)."""
        if self.closed:
            return
        self.closed = True
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            if commit:
                await conn.commit()
            else:
                await conn.rollback()
        except BaseException:
            with contextlib.suppress(psycopg.Error):  # the pool discards broken connections
                await conn.rollback()
            raise
        finally:
            assert self._pool is not None
            await self._pool.putconn(conn)


def get_uow(request: Request) -> UnitOfWork:
    """FastAPI dependency: the request's unit of work."""
    uow: UnitOfWork = request.state.uow
    return uow


async def _send_error(send: Send, mapped: MappedError) -> None:
    body = json.dumps(
        error_body(mapped.code, mapped.message, request_id_var.get(), mapped.constraint)
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": mapped.status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


class UnitOfWorkMiddleware:
    def __init__(self, app: ASGIApp, pool_getter: Callable[[], AsyncConnectionPool | None]) -> None:
        self.app = app
        self.pool_getter = pool_getter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        uow = UnitOfWork(self.pool_getter())
        scope.setdefault("state", {})["uow"] = uow
        replaced = False

        async def send_wrapper(message: Message) -> None:
            nonlocal replaced
            if message["type"] == "http.response.start":
                status: int = message["status"]
                try:
                    await uow.finish(commit=status < 400)
                except psycopg.Error as exc:
                    mapped = map_db_error(exc) or MappedError(500, "internal", "Something went wrong.")
                    log.warning(
                        "commit failed, replacing %d response with %d", status, mapped.status,
                        extra={"sqlstate": exc.sqlstate, "db_error": str(exc)},
                    )  # fmt: skip
                    replaced = True
                    await _send_error(send, mapped)
                    return
            if replaced:
                return  # drop the handler's response; the error was sent instead
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # Exceptions, or an app that never started a response: roll back.
            try:
                await uow.finish(commit=False)
            except psycopg.Error:
                log.exception("rollback failed")

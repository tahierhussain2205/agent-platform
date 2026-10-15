"""FastAPI application factory."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from app import kv
from app.api.middleware import RequestContextMiddleware
from app.context import request_id_var
from app.db.errors import error_body, map_db_error
from app.db.pool import open_pool
from app.db.uow import OrgBindingError, UnitOfWorkClosedError, UnitOfWorkMiddleware
from app.settings import Settings, get_settings
from app.telemetry import configure_tracing, shutdown_tracing, strip_query_strings

log = logging.getLogger(__name__)


@dataclass
class Resources:
    """Process wide clients, created in the lifespan (or injected by tests)."""

    db_pool: AsyncConnectionPool | None = None
    kv: Any = None  # redis.asyncio.Redis
    owned: list[Callable[[], Awaitable[None]]] = field(default_factory=list)


def _error(status: int, code: str, message: str, constraint: str | None = None) -> JSONResponse:
    return JSONResponse(error_body(code, message, request_id_var.get(), constraint), status_code=status)


def create_app(settings: Settings | None = None, resources: Resources | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_tracing("agent-api", settings.otel_exporter_otlp_endpoint, settings.env)
    injected = resources is not None
    res = resources or Resources()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not injected:
            if settings.database_url and settings.db_api_password:
                res.db_pool = await open_pool(
                    settings.component_dsn("api"), "api",
                    min_size=settings.db_pool_min_size, max_size=settings.db_pool_max_size,
                )  # fmt: skip
                res.owned.append(res.db_pool.close)
            if settings.redis_url:
                res.kv = kv.make_client(settings.redis_url.get_secret_value())
                res.owned.append(res.kv.aclose)
        log.info("api started", extra={"env": settings.env})
        try:
            yield
        finally:
            for close in reversed(res.owned):
                await close()
            shutdown_tracing()
            log.info("api stopped")

    app = FastAPI(
        title="Agent Platform API", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.resources = res

    # Middleware order (outermost first): tracing, request context, unit of work.
    app.add_middleware(UnitOfWorkMiddleware, pool_getter=lambda: res.db_pool)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        OpenTelemetryMiddleware, exclude_spans=["receive", "send"], server_request_hook=strip_query_strings
    )

    @app.exception_handler(psycopg.Error)
    async def database_error(request: Request, exc: psycopg.Error) -> JSONResponse:
        mapped = map_db_error(exc)
        if mapped is None:
            if isinstance(exc, psycopg.OperationalError):
                log.warning("database unavailable", extra={"sqlstate": exc.sqlstate})
                return _error(503, "unavailable", "The database is unavailable. Try again.")
            log.exception("unexpected database error")
            return _error(500, "internal", "Something went wrong.")
        log.info("database refused the request", extra={"sqlstate": exc.sqlstate, "db_error": str(exc)})
        return _error(mapped.status, mapped.code, mapped.message, mapped.constraint)

    @app.exception_handler(PoolTimeout)
    async def pool_timeout(request: Request, exc: PoolTimeout) -> JSONResponse:
        log.warning("database pool exhausted")
        return _error(503, "unavailable", "The service is busy. Try again.")

    @app.exception_handler(OrgBindingError)
    @app.exception_handler(UnitOfWorkClosedError)
    async def programming_error(request: Request, exc: Exception) -> JSONResponse:
        log.error("unit of work misuse: %s", exc)
        return _error(500, "internal", "Something went wrong.")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Process is up. Render's health check."""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        """Dependencies reachable: Postgres (as the API's login user) and Key Value."""
        checks: dict[str, str] = {}

        async def check(name: str, configured: bool, probe: Callable[[], Awaitable[object]]) -> None:
            if not configured:
                checks[name] = "not_configured"
                return
            try:
                async with asyncio.timeout(2):
                    await probe()
                checks[name] = "ok"
            except Exception as exc:  # report, never raise: this endpoint must answer
                log.warning("readiness check %s failed: %s", name, type(exc).__name__)
                checks[name] = "unavailable"

        async def db_probe() -> None:
            assert res.db_pool is not None
            async with res.db_pool.connection() as conn:
                await conn.execute("select 1")

        await asyncio.gather(
            check("database", res.db_pool is not None, db_probe),
            check("key_value", res.kv is not None, lambda: kv.ping(res.kv)),
        )
        checks = dict(sorted(checks.items()))  # stable order; the checks run concurrently
        ready = all(v == "ok" for v in checks.values())
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "checks": checks}, status_code=200 if ready else 503
        )

    return app

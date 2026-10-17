"""FastAPI application factory."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware

from app import kv
from app.api.console import autonomy as console_autonomy
from app.api.console import catalog as console_catalog
from app.api.console import integrations as console_integrations
from app.api.console import members as console_members
from app.api.console import org as console_org
from app.api.console import repositories as console_repositories
from app.api.console import roles as console_roles
from app.api.console import schedules as console_schedules
from app.api.console import slack as console_slack
from app.api.console import tasks as console_tasks
from app.api.console import usage as console_usage
from app.api.errors import ApiError
from app.api.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from app.api.oauth import router as oauth_router
from app.api.slack import router as slack_router
from app.api.webhooks import router as webhooks_router
from app.auth.clerk import AuthError, ClerkVerifier
from app.context import request_id_var
from app.db.errors import error_body, map_db_error
from app.db.pool import open_pool
from app.db.uow import OrgBindingError, UnitOfWorkClosedError, UnitOfWorkMiddleware
from app.identity.clerk_api import ClerkClient
from app.settings import Settings, get_settings
from app.slack.client import SlackClient
from app.slack.limiter import SlackRateLimiter
from app.telemetry import configure_tracing, shutdown_tracing, strip_query_strings
from app.vault_service import VaultClient
from app.vault_service.factory import vault_from_settings

log = logging.getLogger(__name__)


@dataclass
class Resources:
    """Process wide clients, created in the lifespan (or injected by tests)."""

    db_pool: AsyncConnectionPool | None = None  # api_service: request paths
    identity_pool: AsyncConnectionPool | None = None  # identity_service: Clerk mirror
    vault_pool: AsyncConnectionPool | None = None  # vault_service: connection secrets only
    vault: VaultClient | None = None
    kv: Any = None  # redis.asyncio.Redis
    clerk_verifier: ClerkVerifier | None = None
    clerk_client: ClerkClient | None = None
    slack: SlackClient | None = None
    temporal: Any = None  # temporalio Client, connected on first use
    owned: list[Callable[[], Awaitable[None]]] = field(default_factory=list)


def _error(
    status: int, code: str, message: str, constraint: str | None = None, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        error_body(code, message, request_id_var.get(), constraint), status_code=status, headers=headers
    )


async def build_resources(settings: Settings, res: Resources) -> None:
    """Open the clients this process needs, based on what is configured."""
    if settings.database_url and settings.db_api_password:
        res.db_pool = await open_pool(
            settings.component_dsn("api"),
            "api",
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
        )
        res.owned.append(res.db_pool.close)
    if settings.database_url and settings.db_identity_password:
        res.identity_pool = await open_pool(
            settings.component_dsn("identity"), "identity", min_size=0, max_size=4
        )
        res.owned.append(res.identity_pool.close)
    if settings.redis_url:
        res.kv = kv.make_client(settings.redis_url.get_secret_value())
        res.owned.append(res.kv.aclose)
    res.vault = await vault_from_settings(settings, res.owned)
    res.slack = SlackClient(
        settings.slack_api_url, limiter=SlackRateLimiter(res.kv) if res.kv is not None else None
    )
    res.owned.append(res.slack.aclose)
    if settings.clerk_issuer and settings.clerk_jwks_url:
        bearer = (
            settings.clerk_secret_key.get_secret_value()
            if settings.clerk_secret_key and "/v1/" in settings.clerk_jwks_url
            else None
        )
        res.clerk_verifier = ClerkVerifier(
            settings.clerk_issuer, settings.clerk_jwks_url, settings.console_origin_list, jwks_bearer=bearer
        )
    if settings.clerk_secret_key:
        res.clerk_client = ClerkClient(
            settings.clerk_api_url,
            settings.clerk_secret_key.get_secret_value(),
            invitation_redirect_url=f"{settings.console_url.rstrip('/')}/sign-up",
        )
        res.owned.append(res.clerk_client.aclose)


def create_app(settings: Settings | None = None, resources: Resources | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_tracing("agent-api", settings.otel_exporter_otlp_endpoint, settings.env)
    injected = resources is not None
    res = resources or Resources()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not injected:
            await build_resources(settings, res)
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

    # Middleware order (outermost first): tracing, CORS, security headers, request context, unit of work.
    app.add_middleware(UnitOfWorkMiddleware, pool_getter=lambda: res.db_pool)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.env in ("staging", "production"))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.console_origin_list,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["authorization", "content-type", "x-request-id"],
        expose_headers=["x-request-id"],
        max_age=600,
    )
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

    @app.exception_handler(AuthError)
    async def auth_error(request: Request, exc: AuthError) -> JSONResponse:
        headers = {"WWW-Authenticate": 'Bearer error="invalid_token"'} if exc.status == 401 else None
        return _error(exc.status, exc.code, exc.message, headers=headers)

    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()
        ]
        body = error_body(
            "invalid", fields[0]["message"] if fields else "Invalid request.", request_id_var.get()
        )
        body["error"]["fields"] = fields  # type: ignore[index]
        return JSONResponse(body, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed"}
        return _error(
            exc.status_code,
            codes.get(exc.status_code, "error"),
            str(exc.detail),
            headers=getattr(exc, "headers", None),
        )

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

    app.include_router(webhooks_router)
    app.include_router(console_org.router)
    app.include_router(console_members.router)
    app.include_router(console_roles.router)
    app.include_router(console_slack.router)
    app.include_router(console_integrations.router)
    app.include_router(console_schedules.router)
    app.include_router(console_catalog.router)
    app.include_router(console_repositories.router)
    app.include_router(console_usage.router)
    app.include_router(console_autonomy.router)
    app.include_router(console_tasks.router)
    app.include_router(slack_router)
    app.include_router(oauth_router)
    return app

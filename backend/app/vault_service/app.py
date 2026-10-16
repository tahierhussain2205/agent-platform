"""The vault's HTTP interface (``app vault``), reachable only on the private network.

Every call carries the shared service token and names its org; the work runs in that org's tenant
transactions as ``vault_service``. Errors keep Slack style codes the clients map back to
``VaultError``. Secrets are never logged.
"""

import hmac
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app.settings import Settings
from app.vault_service import LocalVault, VaultError

log = logging.getLogger(__name__)


class Scoped(BaseModel):
    org_id: UUID
    connection_id: UUID


class StoreBody(Scoped):
    secret: str


class ExchangeBody(Scoped):
    provider: str
    code: str
    redirect_uri: str
    verifier: str | None = None


class InstallationBody(Scoped):
    installation_id: str
    code: str
    redirect_uri: str


class CredentialBody(Scoped):
    task_id: UUID | None = None
    membership_id: UUID | None = None


def create_vault_app(settings: Settings, vault: LocalVault | None = None) -> FastAPI:
    state: dict[str, Any] = {"vault": vault}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        closers: list[Callable[[], Awaitable[None]]] = []
        if state["vault"] is None:
            from app.crypto import Envelope, LocalKeyProvider
            from app.db.pool import open_pool
            from app.integrations.providers import providers

            if not (settings.encryption_keys and settings.encryption_active_key):
                raise RuntimeError("ENCRYPTION_KEYS and ENCRYPTION_ACTIVE_KEY are required for the vault")
            pool = await open_pool(
                settings.component_dsn("vault"), "vault", min_size=1, max_size=settings.db_pool_max_size
            )
            closers.append(pool.close)
            keys = LocalKeyProvider.from_config(
                settings.encryption_keys.get_secret_value(), settings.encryption_active_key
            )
            local = LocalVault(pool, Envelope(keys), providers(settings))
            closers.append(local.aclose)
            state["vault"] = local
        yield
        for close in closers:
            await close()

    app = FastAPI(title="vault", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    token = settings.vault_service_token.get_secret_value() if settings.vault_service_token else None

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Any) -> Response:
        if request.url.path == "/healthz":
            return await call_next(request)  # type: ignore[no-any-return]
        started = time.monotonic()
        sent = request.headers.get("authorization", "")
        if not token or not hmac.compare_digest(sent, f"Bearer {token}"):
            response: Response = JSONResponse({"error": "unauthorized"}, status_code=401)
        else:
            response = await call_next(request)
        log.info(  # one line per call; never bodies (they carry secrets)
            "vault request",
            extra={
                "http_path": request.url.path,
                "http_status": response.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
            },
        )
        return response

    @app.exception_handler(VaultError)
    async def vault_error(request: Request, exc: VaultError) -> JSONResponse:
        log.info("vault refused", extra={"code": exc.code, "path": request.url.path})
        return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status)

    def v() -> LocalVault:
        vault_: LocalVault = state["vault"]
        return vault_

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/ready")
    async def ready() -> dict[str, str]:
        """Authenticated, and connects as the vault database user: what callers depend on."""
        async with v().pool.connection() as conn:
            await conn.execute("select 1")
        return {"status": "ready"}

    @app.post("/v1/secrets")
    async def store(body: StoreBody) -> Response:
        await v().store(body.org_id, body.connection_id, body.secret)
        return Response(status_code=204)

    @app.post("/v1/secrets/read")
    async def read(body: Scoped) -> dict[str, str]:
        return {"secret": await v().read(body.org_id, body.connection_id)}

    @app.post("/v1/secrets/delete")
    async def delete(body: Scoped) -> Response:
        await v().delete(body.org_id, body.connection_id)
        return Response(status_code=204)

    @app.post("/v1/exchange")
    async def exchange(body: ExchangeBody) -> dict[str, Any]:
        return await v().exchange(
            body.org_id, body.connection_id, body.provider, body.code, body.redirect_uri, body.verifier
        )

    @app.post("/v1/github/installations")
    async def installation(body: InstallationBody) -> dict[str, Any]:
        return await v().register_installation(
            body.org_id, body.connection_id, body.installation_id, body.code, body.redirect_uri
        )

    @app.post("/v1/credentials")
    async def credential(body: CredentialBody) -> dict[str, Any]:
        issued = await v().credential(body.org_id, body.connection_id, body.task_id, body.membership_id)
        log.info(
            "credential issued",
            extra={"kind": issued.kind, "scope": issued.scope, "task": str(body.task_id or "")},
        )
        return issued.to_dict()

    @app.post("/v1/revoke")
    async def revoke(body: Scoped) -> Response:
        await v().revoke(body.org_id, body.connection_id)
        return Response(status_code=204)

    return app

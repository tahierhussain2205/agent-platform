"""OAuth callbacks for integration connect (admins, org connections) and link (anyone, their own
account) flows, and the GitHub App's setup URL.

The state is signed, expires and is single use (a Key Value claim on its nonce). The connection row
is written and committed first (the vault, on its own database connection, must see it), then the
vault exchanges the code; a failed exchange revokes the row. Reconnecting replaces the previous
connection (one active org connection per integration, one link per person).
"""

import logging
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from psycopg.types.json import Jsonb

from app import kv
from app.auth.console import resources
from app.db.tenant import tenant_tx
from app.integrations.catalog import CATALOG
from app.integrations.state import StateError, read_state
from app.settings import get_settings
from app.vault_service import VaultError

log = logging.getLogger(__name__)
router = APIRouter()
PKCE_KEY = "oauth-pkce"
NONCE_KEY = "oauth-state"


def callback_url(key: str) -> str:
    return f"{get_settings().api_public_url}/oauth/{key}/callback"


def install_url() -> str:
    return f"{get_settings().api_public_url}/oauth/github/install"


def _back(**params: str) -> RedirectResponse:
    return RedirectResponse(f"{get_settings().console_url}/integrations?{urlencode(params)}", status_code=302)


@router.get("/oauth/github/install", include_in_schema=False)
async def github_install(request: Request) -> RedirectResponse:
    """The GitHub App's setup URL: after installing, with the installer's user authorization code."""
    p = request.query_params
    if p.get("setup_action") not in ("install", "update") or not p.get("installation_id"):
        return _back(error=p.get("error") or "github_install_cancelled")
    return await _finish(
        request, "github", p.get("code"), p.get("state"), installation_id=p.get("installation_id")
    )


@router.get("/oauth/{key}/callback", include_in_schema=False)
async def oauth_callback(request: Request, key: str) -> RedirectResponse:
    p = request.query_params
    if p.get("error"):
        return _back(error="access_denied" if p.get("error") == "access_denied" else "provider_error")
    return await _finish(request, key, p.get("code"), p.get("state"))


async def _finish(
    request: Request, key: str, code: str | None, state: str | None, installation_id: str | None = None
) -> RedirectResponse:
    settings = get_settings()
    res = resources(request)
    integration = CATALOG.get(key)
    if integration is None or not integration.connectable or res.db_pool is None or res.vault is None:
        return _back(error="not_configured")
    if settings.oauth_state_secret is None or not code or not state:
        return _back(error="bad_state")
    try:
        flow = read_state(settings.oauth_state_secret.get_secret_value(), state)
    except StateError as exc:
        return _back(error=exc.code)
    if flow.integration != key or (installation_id is not None) != (key == "github" and flow.scope == "org"):
        return _back(error="bad_state")
    if res.kv is None or not await kv.claim(res.kv, kv.global_key(NONCE_KEY, flow.nonce), 600):
        return _back(error="state_reused")
    verifier = await res.kv.get(kv.global_key(PKCE_KEY, flow.nonce))
    org_id, member = UUID(flow.org_id), UUID(flow.membership_id)
    user = flow.scope == "user"
    async with tenant_tx(res.db_pool, org_id) as conn:
        row = await (
            await conn.execute("select privilege, status from app.org_memberships where id = %s", (member,))
        ).fetchone()
        if row is None or row[1] == "removed" or (not user and (row[0] != "admin" or row[1] != "active")):
            return _back(error="not_allowed")
        previous = await (
            await conn.execute(
                "update app.connections set status = 'revoked' where integration_key = %s and status = "
                "'active' "
                "and (case when %s then scope = 'user' and membership_id = %s else scope = 'org' end) "
                "returning id",
                (key, user, member),
            )
        ).fetchall()
        await conn.execute(
            "insert into app.org_integrations (org_id, integration_key, kind, enabled) values (%s, %s, "
            "%s, %s) "
            "on conflict (org_id, integration_key) do update "
            "set enabled = app.org_integrations.enabled or excluded.enabled",
            (org_id, key, integration.kind, not user),
        )
        new = await (
            await conn.execute(
                "insert into app.connections (org_id, integration_key, scope, membership_id) "
                "values (%s, %s, %s, %s) returning id",
                (org_id, key, flow.scope, member if user else None),
            )
        ).fetchone()
        assert new is not None
        connection_id = new[0]
    try:
        if installation_id is not None:
            result = await res.vault.register_installation(
                org_id, connection_id, installation_id, code, install_url()
            )
        else:
            result = await res.vault.exchange(
                org_id,
                connection_id,
                integration.provider or key,
                code,
                callback_url(key),
                verifier.decode() if isinstance(verifier, bytes) else verifier,
            )
    except VaultError as exc:
        log.warning("integration connect failed", extra={"integration": key, "code": exc.code})
        async with tenant_tx(res.db_pool, org_id) as conn:
            await conn.execute(
                "update app.connections set status = 'revoked' where id = %s", (connection_id,)
            )
        return _back(error="not_yours" if exc.code == "forbidden" else "exchange_failed", integration=key)
    for (old,) in previous:
        try:
            await res.vault.delete(org_id, old)
        except VaultError:
            log.info("old connection secret already gone")
    await _audit(res, org_id, member, f"integration.{'linked' if user else 'connected'}", key, result)
    return _back(**{"linked" if user else "connected": key})


async def _audit(res: Any, org_id: UUID, member: UUID, action: str, key: str, result: dict[str, Any]) -> None:
    async with tenant_tx(res.db_pool, org_id) as conn:
        await conn.execute(
            "insert into app.audit_log (org_id, actor_type, actor_id, action, target, metadata) "
            "values (%s, 'user', %s, %s, %s, %s)",
            (org_id, member, action, key, Jsonb({"account": result.get("external_account_id")})),
        )

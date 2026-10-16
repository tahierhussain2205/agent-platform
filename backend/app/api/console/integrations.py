"""Console: integrations. Enable tools, connect org accounts (admins), link your own account
(everyone), credential mode and enablement overrides per operation (admins), customer supplied MCP
servers behind admin approval, and connection health."""

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from app import kv
from app.api.console.common import audit, fetch_all, fetch_one
from app.api.errors import ApiError
from app.api.oauth import PKCE_KEY, callback_url
from app.auth.console import ConsoleIdentity, console_identity, require_admin, resources
from app.db.tenant import tenant_tx
from app.db.uow import UnitOfWork, get_uow
from app.integrations.catalog import CATALOG, custom_key, is_custom
from app.integrations.providers import pkce_pair, providers
from app.integrations.state import make_state
from app.settings import Settings, get_settings
from app.tools.base import CredentialMode, Operation
from app.vault_service import VaultError

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/integrations")
MODES = ("org", "org_with_check", "user_required")


def operations(key: str, config: dict[str, Any]) -> list[Operation]:
    """Operation metadata: in code for native adapters, discovered for MCP servers."""
    from app.tools.github import OPERATIONS as GITHUB
    from app.tools.jira import OPERATIONS as JIRA
    from app.tools.mcp import operation_for
    from app.tools.slack import OPERATIONS as SLACK

    native = {"slack": SLACK, "jira": JIRA, "github": GITHUB}
    if key in native:
        return list(native[key].values())
    mode: CredentialMode = "user_required" if key == "google_workspace" else "org"
    return [operation_for(t, mode) for t in config.get("tools") or []]


def configured(settings: Settings, key: str, config: dict[str, Any]) -> bool:
    return {
        "slack": bool(settings.slack_client_id),
        "jira": bool(settings.atlassian_client_id),
        "github": bool(settings.github_app_slug and settings.github_client_id),
        "google_workspace": bool(settings.google_client_id and settings.google_mcp_url),
    }.get(key, bool(config.get("url")))


def _connection_json(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        k: row[k]
        for k in ("id", "status", "external_account_id", "created_at", "expires_at", "last_refreshed_at")
    }


@router.get("")
async def list_integrations(
    identity: ConsoleIdentity = Depends(console_identity), uow: UnitOfWork = Depends(get_uow)
) -> dict[str, Any]:
    conn, settings = uow.connection, get_settings()
    rows = {
        r["integration_key"]: r
        for r in await fetch_all(
            conn,
            "select integration_key, kind, enabled, health, config, approved_at from app.org_integrations",
        )
    }
    connections = await fetch_all(
        conn,
        "select id, integration_key, scope, membership_id, status, external_account_id, created_at, "
        "expires_at, "
        "last_refreshed_at from app.connections where status in ('active', 'expired') "
        "and (scope = 'org' or membership_id = %s) order by created_at desc",
        (identity.membership_id,),
    )
    overrides = {
        (r["integration_key"], r["operation"]): r
        for r in await fetch_all(
            conn, "select integration_key, operation, credential_mode, enabled from app.operation_policies"
        )
    }
    keys = [*CATALOG, *sorted(k for k in rows if is_custom(k))]
    out = []
    for key in keys:
        row = rows.get(key) or {}
        config = row.get("config") or {}
        catalog = CATALOG.get(key)
        org_conn = next((c for c in connections if c["integration_key"] == key and c["scope"] == "org"), None)
        link = next((c for c in connections if c["integration_key"] == key and c["scope"] == "user"), None)
        ops = []
        for op in operations(key, config):
            override = overrides.get((key, op.name))
            ops.append(
                {
                    "name": op.name,
                    "description": op.description,
                    "tier": op.tier,
                    "default_mode": op.credential_mode,
                    "mode": override["credential_mode"] if override else op.credential_mode,
                    "enabled": override["enabled"] if override else True,
                    "overridden": override is not None,
                }
            )
        out.append(
            {
                "key": key,
                "title": catalog.title if catalog else str(config.get("title") or key),
                "kind": catalog.kind if catalog else "custom_mcp",
                "auth": catalog.auth if catalog else ("api_key" if org_conn else "none"),
                "description": catalog.description
                if catalog
                else f"Customer MCP server at {config.get('url')}",
                "user_links": bool(catalog and catalog.user_links),
                "enabled": bool(row.get("enabled")),
                "health": row.get("health") or "unknown",
                "approved": key not in rows or not is_custom(key) or row.get("approved_at") is not None,
                "configured": configured(settings, key, config),
                "connection": _connection_json(org_conn),
                "link": _connection_json(link),
                "operations": ops,
            }
        )
    return {"integrations": out}


async def _row(conn: Any, key: str) -> dict[str, Any] | None:
    return await fetch_one(
        conn,
        "select integration_key, kind, enabled, approved_at, config from app.org_integrations "
        "where integration_key = %s",
        (key,),
    )


def _known(key: str, row: dict[str, Any] | None) -> None:
    if key not in CATALOG and not (is_custom(key) and row):
        raise ApiError(404, "unknown_integration", "There is no such integration.")


class EnableBody(BaseModel):
    enabled: bool


@router.patch("/{key}")
async def set_enabled(
    key: str,
    body: EnableBody,
    identity: ConsoleIdentity = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    row = await _row(uow.connection, key)
    _known(key, row)
    if key == "slack":
        raise ApiError(409, "managed_elsewhere", "Slack is connected and disconnected from the Slack page.")
    if body.enabled and is_custom(key) and row and row["approved_at"] is None:
        raise ApiError(
            409, "approval_required", "An admin has to approve this MCP server before it is enabled."
        )
    # An update for existing rows: Postgres checks an insert's constraints before ON CONFLICT
    # would turn it into an update, and an unapproved custom server row would fail them.
    if row is not None:
        await uow.connection.execute(
            "update app.org_integrations set enabled = %s where integration_key = %s", (body.enabled, key)
        )
    else:
        await uow.connection.execute(
            "insert into app.org_integrations (org_id, integration_key, kind, enabled) values (%s, %s, "
            "%s, %s)",
            (identity.org_id, key, CATALOG[key].kind, body.enabled),
        )
    await audit(uow.connection, identity, f"integration.{'enabled' if body.enabled else 'disabled'}", key)
    return {"key": key, "enabled": body.enabled}


async def _begin(request: Request, identity: ConsoleIdentity, key: str, scope: str) -> dict[str, str]:
    settings, res = get_settings(), resources(request)
    integration = CATALOG.get(key)
    if integration is None or not integration.connectable:
        raise ApiError(404, "not_connectable", "This integration is not connected through OAuth.")
    if scope == "user" and not integration.user_links:
        raise ApiError(409, "no_links", "People do not link their own accounts for this integration.")
    provider = providers(settings).get(integration.provider or key)
    if provider is None or not provider.configured or settings.oauth_state_secret is None or res.kv is None:
        raise ApiError(503, "not_configured", f"{integration.title} is not configured for this environment.")
    state, nonce = make_state(
        settings.oauth_state_secret.get_secret_value(), identity.org_id, identity.membership_id, key, scope
    )
    challenge = None
    if provider.uses_pkce:
        verifier, challenge = pkce_pair()
        await res.kv.set(kv.global_key(PKCE_KEY, nonce), verifier, ex=600)
    url = provider.authorize_url(state, callback_url(key), challenge, scope)  # the callback claims the nonce
    await provider.http.aclose()
    return {"url": url}


@router.get("/{key}/connect")
async def connect(
    key: str, request: Request, identity: ConsoleIdentity = Depends(require_admin)
) -> dict[str, str]:
    return await _begin(request, identity, key, "org")


@router.get("/{key}/link")
async def link(
    key: str,
    request: Request,
    identity: ConsoleIdentity = Depends(console_identity),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, str]:
    row = await _row(uow.connection, key)
    if row is None or not row["enabled"]:
        raise ApiError(409, "not_enabled", "An admin has to enable this integration first.")
    return await _begin(request, identity, key, "user")


async def _revoke(request: Request, identity: ConsoleIdentity, uow: UnitOfWork, key: str, user: bool) -> None:
    if user:
        row = await fetch_one(
            uow.connection,
            "select id from app.connections where integration_key = %s and status in ('active', 'expired') "
            "and scope = 'user' and membership_id = %s",
            (key, identity.membership_id),
        )
    else:
        row = await fetch_one(
            uow.connection,
            "select id from app.connections where integration_key = %s and status in ('active', 'expired') "
            "and scope = 'org'",
            (key,),
        )
    if row is None:
        raise ApiError(404, "not_connected", "There is nothing to disconnect.")
    vault = resources(request).vault
    if vault is None:
        raise ApiError(503, "vault_unavailable", "The token vault is not configured.")
    try:
        await vault.revoke(identity.org_id, row["id"])
    except VaultError as exc:
        raise ApiError(502, "revoke_failed", f"Could not revoke the connection: {exc.message}") from exc
    await audit(uow.connection, identity, f"integration.{'unlinked' if user else 'disconnected'}", key)


@router.delete("/{key}/connection", status_code=204)
async def disconnect(
    key: str,
    request: Request,
    identity: ConsoleIdentity = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> None:
    await _revoke(request, identity, uow, key, user=False)


@router.delete("/{key}/link", status_code=204)
async def unlink(
    key: str,
    request: Request,
    identity: ConsoleIdentity = Depends(console_identity),
    uow: UnitOfWork = Depends(get_uow),
) -> None:
    await _revoke(request, identity, uow, key, user=True)


class OverrideBody(BaseModel):
    credential_mode: str = Field(pattern="^(org|org_with_check|user_required)$")
    enabled: bool = True


@router.put("/{key}/operations/{operation}")
async def set_override(
    key: str,
    operation: str,
    body: OverrideBody,
    identity: ConsoleIdentity = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    row = await _row(uow.connection, key)
    _known(key, row)
    if operation not in {op.name for op in operations(key, (row or {}).get("config") or {})}:
        raise ApiError(404, "unknown_operation", "There is no such operation.")
    if body.credential_mode == "user_required" and not (key in CATALOG and CATALOG[key].user_links):
        raise ApiError(409, "no_links", "Nobody links their own account for this integration.")
    kind = CATALOG[key].kind if key in CATALOG else "custom_mcp"
    await uow.connection.execute(
        "insert into app.org_integrations (org_id, integration_key, kind) values (%s, %s, %s) "
        "on conflict (org_id, integration_key) do nothing",
        (identity.org_id, key, kind),
    )
    await uow.connection.execute(
        "insert into app.operation_policies (org_id, integration_key, operation, credential_mode, "
        "enabled, updated_by) "
        "values (%s, %s, %s, %s, %s, %s) on conflict (org_id, integration_key, operation) do update "
        "set credential_mode = excluded.credential_mode, enabled = excluded.enabled, updated_by = "
        "excluded.updated_by",
        (identity.org_id, key, operation, body.credential_mode, body.enabled, identity.membership_id),
    )
    await audit(
        uow.connection,
        identity,
        "integration.operation_policy",
        f"{key}.{operation}",
        {"credential_mode": body.credential_mode, "enabled": body.enabled},
    )
    return {
        "key": key,
        "operation": operation,
        "credential_mode": body.credential_mode,
        "enabled": body.enabled,
    }


@router.delete("/{key}/operations/{operation}", status_code=204)
async def clear_override(
    key: str,
    operation: str,
    identity: ConsoleIdentity = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> None:
    await uow.connection.execute(
        "delete from app.operation_policies where integration_key = %s and operation = %s", (key, operation)
    )
    await audit(uow.connection, identity, "integration.operation_policy_cleared", f"{key}.{operation}")


class CustomMcpBody(BaseModel):
    title: str = Field(min_length=1, max_length=60)
    url: str = Field(min_length=8, max_length=500)
    api_key: str | None = Field(default=None, max_length=2000)


@router.post("/custom-mcp", status_code=201)
async def add_custom_mcp(
    body: CustomMcpBody, request: Request, identity: ConsoleIdentity = Depends(require_admin)
) -> dict[str, Any]:
    """Added disabled and unapproved; an admin approves it after checking what it is."""
    settings, res = get_settings(), resources(request)
    local = settings.env in ("local", "test")
    if not (body.url.startswith("https://") or (local and body.url.startswith("http://"))):
        raise ApiError(422, "insecure_url", "MCP servers must be reached over https.")
    if res.db_pool is None or res.vault is None:
        raise ApiError(503, "vault_unavailable", "The token vault is not configured.")
    key = custom_key(body.title)
    async with tenant_tx(res.db_pool, identity.org_id) as conn:  # committed before the vault stores the key
        exists = await (
            await conn.execute("select 1 from app.org_integrations where integration_key = %s", (key,))
        ).fetchone()
        if exists:
            raise ApiError(409, "exists", "An MCP server with this name exists already.")
        await conn.execute(
            "insert into app.org_integrations (org_id, integration_key, kind, enabled, config) "
            "values (%s, %s, 'custom_mcp', false, %s)",
            (identity.org_id, key, Jsonb({"url": body.url, "title": body.title})),
        )
        connection = await (
            await conn.execute(
                "insert into app.connections (org_id, integration_key, scope) values (%s, %s, 'org') "
                "returning id",
                (identity.org_id, key),
            )
        ).fetchone()
        await conn.execute(
            "insert into app.audit_log (org_id, actor_type, actor_id, action, target) "
            "values (%s, 'user', %s, 'integration.custom_mcp_added', %s)",
            (identity.org_id, identity.membership_id, key),
        )
    assert connection is not None
    try:
        await res.vault.store(identity.org_id, connection[0], body.api_key or "")
    except VaultError as exc:
        raise ApiError(502, "vault_failed", exc.message) from exc
    return {"key": key, "enabled": False, "approved": False}


@router.post("/{key}/approve")
async def approve(
    key: str, request: Request, identity: ConsoleIdentity = Depends(require_admin)
) -> dict[str, Any]:
    res = resources(request)
    assert res.db_pool is not None
    async with tenant_tx(res.db_pool, identity.org_id) as conn:
        updated = await (
            await conn.execute(
                "update app.org_integrations set approved_by = %s, approved_at = now() "
                "where integration_key = %s and kind = 'custom_mcp' and approved_at is null returning "
                "integration_key",
                (identity.membership_id, key),
            )
        ).fetchone()
        if updated is None:
            raise ApiError(404, "not_pending", "There is no MCP server waiting for approval with this name.")
        await conn.execute(
            "insert into app.audit_log (org_id, actor_type, actor_id, action, target, decision) "
            "values (%s, 'user', %s, 'integration.custom_mcp_approved', %s, 'allowed')",
            (identity.org_id, identity.membership_id, key),
        )
    return await _probe(request, identity, key)


@router.post("/{key}/check")
async def check(
    key: str, request: Request, identity: ConsoleIdentity = Depends(require_admin)
) -> dict[str, Any]:
    return await _probe(request, identity, key)


async def _probe(request: Request, identity: ConsoleIdentity, key: str) -> dict[str, Any]:
    """A cheap call with the org connection (or the admin's own link where the integration works with
    links only); records health, and caches an MCP server's tools."""
    from app.tools.mcp import McpClient

    settings, res = get_settings(), resources(request)
    assert res.db_pool is not None
    assert res.vault is not None
    async with tenant_tx(res.db_pool, identity.org_id) as conn:
        row = await (
            await conn.execute(
                "select c.id, i.config from app.connections c join app.org_integrations i "
                "on i.org_id = c.org_id and i.integration_key = c.integration_key "
                "where c.integration_key = %s and c.status = 'active' and (c.scope = 'org' or "
                "c.membership_id = %s) "
                "order by c.scope limit 1",
                (key, identity.membership_id),
            )
        ).fetchone()
    if row is None:
        raise ApiError(404, "not_connected", "Connect it first.")
    connection_id, config = row[0], row[1] or {}
    health, detail, tools = "ok", "The connection works.", None
    try:
        credential = await res.vault.credential(
            identity.org_id, connection_id, membership_id=identity.membership_id
        )
        async with httpx.AsyncClient(timeout=15) as http:
            headers = {"authorization": f"Bearer {credential.token}"}
            if key == "jira":
                response = await http.get(
                    f"{settings.atlassian_api_url}/ex/jira/{credential.details.get('cloud_id')}/rest/api/3/myself",
                    headers=headers,
                )
                response.raise_for_status()
            elif key == "github":
                response = await http.get(
                    f"{settings.github_api_url}/installation/repositories", headers=headers
                )
                response.raise_for_status()
                detail = f"{response.json().get('total_count', 0)} repositories visible."
            else:
                url = settings.google_mcp_url if key == "google_workspace" else config.get("url")
                tools = await McpClient(http).list_tools(str(url), credential.token)
                detail = f"{len(tools)} tools available."
    except Exception as exc:  # any failure means broken health
        health, detail = "broken", str(getattr(exc, "message", exc))[:300] or type(exc).__name__
    async with tenant_tx(res.db_pool, identity.org_id) as conn:
        await conn.execute(
            "update app.org_integrations set health = %s, config = case when %s::jsonb is null then config "
            "else config || jsonb_build_object('tools', %s::jsonb) end where integration_key = %s",
            (
                health,
                Jsonb(tools) if tools is not None else None,
                Jsonb(tools) if tools is not None else None,
                key,
            ),
        )
    return {"key": key, "health": health, "detail": detail}

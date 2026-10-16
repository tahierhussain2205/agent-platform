"""The token vault (spec 6.1, 12.4): holds every integration secret and issues short lived,
task scoped credentials. Runs as its own service (``app vault``) with its own database user
(``vault_service``, role ``app_vault``) and the providers' client secrets; the API and worker reach
it through ``RemoteVault`` and never see refresh tokens or app keys.

``LocalVault`` is the implementation (also usable in process, for tests and as a fallback):
- secrets are envelope encrypted per tenant (``app.vault.Vault``);
- OAuth connections store a token set; credentials are access tokens, refreshed under a per
  connection advisory lock when near expiry (rotating refresh tokens are stored at once); a failed
  refresh marks the connection expired and the integration's health broken;
- GitHub App installations store the installation id; credentials are one hour installation
  tokens minted with the app key;
- static secrets (Slack bot token, custom MCP API keys) are returned as they are, and are the only
  secrets ``read`` returns;
- a credential for a user link is issued only for a running task its owner requested (or for the
  owner themselves); every request runs in a tenant transaction for the named org.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from app.crypto import Envelope
from app.crypto.envelope import DecryptionError
from app.db.tenant import tenant_tx
from app.integrations.providers import (
    Account,
    GitHubProvider,
    GoogleCloudProvider,
    OAuthProvider,
    ProviderError,
    TokenSet,
    token_dict,
)
from app.vault import SecretNotFoundError, Vault

log = logging.getLogger(__name__)
REFRESH_MARGIN = 120.0
RUNNING = ("running", "awaiting_approval")


class VaultError(Exception):
    STATUS: ClassVar[dict[str, int]] = {
        "not_found": 404,
        "not_active": 409,
        "forbidden": 403,
        "provider": 502,
        "bad_request": 400,
    }

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def status(self) -> int:
        return self.STATUS.get(self.code, 400)


@dataclass(frozen=True)
class Credential:
    token: str
    kind: str  # oauth2 | github_installation | gcp_service_account | static
    connection_id: str
    scope: str  # org | user
    expires_at: float | None = None
    account_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Credential":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


class VaultClient(Protocol):
    """What the API and the worker use (``LocalVault`` or ``RemoteVault``)."""

    async def store(self, org_id: UUID | str, connection_id: UUID | str, secret: str) -> None: ...

    async def read(self, org_id: UUID | str, connection_id: UUID | str) -> str: ...

    async def delete(self, org_id: UUID | str, connection_id: UUID | str) -> None: ...

    async def exchange(
        self,
        org_id: UUID | str,
        connection_id: UUID | str,
        provider: str,
        code: str,
        redirect_uri: str,
        verifier: str | None = None,
    ) -> dict[str, Any]: ...

    async def register_installation(
        self,
        org_id: UUID | str,
        connection_id: UUID | str,
        installation_id: str,
        code: str,
        redirect_uri: str,
    ) -> dict[str, Any]: ...

    async def register_service_account(
        self, org_id: UUID | str, connection_id: UUID | str, key_json: str
    ) -> dict[str, Any]: ...

    async def credential(
        self,
        org_id: UUID | str,
        connection_id: UUID | str,
        task_id: UUID | str | None = None,
        membership_id: UUID | str | None = None,
    ) -> Credential: ...

    async def revoke(self, org_id: UUID | str, connection_id: UUID | str) -> None: ...


def _parse(secret: str) -> dict[str, Any]:
    try:
        data = json.loads(secret)
    except ValueError:
        return {"kind": "static", "token": secret}
    return data if isinstance(data, dict) and "kind" in data else {"kind": "static", "token": secret}


class LocalVault:
    def __init__(
        self, pool: AsyncConnectionPool, envelope: Envelope, providers: dict[str, OAuthProvider] | None = None
    ) -> None:
        self.pool = pool
        self.store_ = Vault(pool, envelope, cache_seconds=0)
        self.providers = providers or {}
        self._installation_tokens: dict[str, tuple[str, float]] = {}
        self._service_account_tokens: dict[str, tuple[str, float]] = {}

    async def aclose(self) -> None:
        for client in {id(p.http): p.http for p in self.providers.values()}.values():
            await client.aclose()

    # ------------------------------------------------------------------ raw secrets

    async def store(self, org_id: UUID | str, connection_id: UUID | str, secret: str) -> None:
        await self.store_.store(org_id, connection_id, secret)

    async def read(self, org_id: UUID | str, connection_id: UUID | str) -> str:
        """Static secrets only: token sets and installations never leave the vault."""
        try:
            data = _parse(await self.store_.read(org_id, connection_id))
        except SecretNotFoundError as exc:
            raise VaultError("not_found", "no secret for this connection") from exc
        if data["kind"] != "static":
            raise VaultError("forbidden", "only static secrets can be read; ask for a credential")
        return str(data["token"])

    async def delete(self, org_id: UUID | str, connection_id: UUID | str) -> None:
        await self.store_.delete(org_id, connection_id)
        self._installation_tokens.pop(str(connection_id), None)
        self._service_account_tokens.pop(str(connection_id), None)

    # ------------------------------------------------------------------ connection rows

    async def _connection(self, org_id: UUID | str, connection_id: UUID | str) -> dict[str, Any]:
        async with tenant_tx(self.pool, org_id) as conn:
            row = await (
                await conn.execute(
                    "select integration_key, scope, membership_id, status, external_account_id "
                    "from app.connections where id = %s",
                    (connection_id,),
                )
            ).fetchone()
        if row is None:
            raise VaultError("not_found", "connection not found")
        return {
            "integration": row[0],
            "scope": row[1],
            "membership_id": row[2],
            "status": row[3],
            "account": row[4],
        }

    async def _record(
        self, org_id: UUID | str, connection_id: UUID | str, account: Account, tokens: TokenSet | None
    ) -> None:
        async with tenant_tx(self.pool, org_id) as conn:
            await conn.execute(
                "update app.connections set external_account_id = %s, external_account_label = %s, "
                "granted_scopes = %s, expires_at = to_timestamp(%s), last_refreshed_at = now() where id = %s",
                (
                    account.external_id,
                    account.label[:200] or None,
                    tokens.scope.split() if tokens else [],
                    tokens.expires_at if tokens else None,
                    connection_id,
                ),
            )

    async def _expire(
        self, org_id: UUID | str, connection_id: UUID | str, integration: str, reason: str
    ) -> None:
        log.warning("connection expired", extra={"integration": integration, "reason": reason})
        async with tenant_tx(self.pool, org_id) as conn:
            await conn.execute(
                "update app.connections set status = 'expired' where id = %s and status = 'active'",
                (connection_id,),
            )
            await conn.execute(
                "update app.org_integrations set health = 'broken' where integration_key = %s", (integration,)
            )

    def _provider(self, key: str) -> OAuthProvider:
        provider = self.providers.get(key)
        if provider is None:
            raise VaultError("bad_request", f"no OAuth provider for {key}")
        return provider

    # ------------------------------------------------------------------ connecting

    async def exchange(
        self,
        org_id: UUID | str,
        connection_id: UUID | str,
        provider: str,
        code: str,
        redirect_uri: str,
        verifier: str | None = None,
    ) -> dict[str, Any]:
        row = await self._connection(org_id, connection_id)
        if row["status"] != "active" or row["integration"] != provider:
            raise VaultError("not_active", "the connection is not waiting for this provider")
        oauth = self._provider(provider)
        try:
            tokens = await oauth.exchange(code, redirect_uri, verifier)
            account = await oauth.account(tokens, row["scope"])
        except ProviderError as exc:
            raise VaultError("provider", f"{provider}: {exc.message}") from exc
        if provider == "jira" and row["scope"] == "user":
            await self._same_jira_site(org_id, account)
        secret = {"kind": "oauth2", "provider": provider, **token_dict(tokens), "details": account.details}
        await self.store_.store(org_id, connection_id, json.dumps(secret))
        await self._record(org_id, connection_id, account, tokens)
        return {"external_account_id": account.external_id, "label": account.label}

    async def _same_jira_site(self, org_id: UUID | str, account: Account) -> None:
        async with tenant_tx(self.pool, org_id) as conn:
            row = await (
                await conn.execute(
                    "select external_account_id from app.connections where integration_key = 'jira' "
                    "and scope = 'org' and status = 'active'"
                )
            ).fetchone()
        if row and row[0] and row[0] != account.details.get("cloud_id"):
            raise VaultError(
                "forbidden", "Your Atlassian account is on a different Jira site than the organization's."
            )

    async def register_installation(
        self,
        org_id: UUID | str,
        connection_id: UUID | str,
        installation_id: str,
        code: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        row = await self._connection(org_id, connection_id)
        if row["status"] != "active" or row["integration"] != "github" or row["scope"] != "org":
            raise VaultError("not_active", "the connection is not waiting for a GitHub installation")
        github = self._provider("github")
        assert isinstance(github, GitHubProvider)
        try:
            user = await github.exchange(code, redirect_uri, None)
            installations = await github.user_installations(user.access_token)
            match = next((i for i in installations if str(i.get("id")) == str(installation_id)), None)
            if match is None:
                raise VaultError("forbidden", "That GitHub App installation does not belong to you.")
            await github.installation_token(installation_id)  # proves the app can use it
            try:
                await github.revoke(user)  # the installer's own token is not needed any more
            except Exception:  # best effort
                log.info("could not revoke the installer's token")
        except ProviderError as exc:
            raise VaultError("provider", f"github: {exc.message}") from exc
        login = str((match.get("account") or {}).get("login") or installation_id)
        secret = {"kind": "github_installation", "installation_id": str(installation_id), "account": login}
        await self.store_.store(org_id, connection_id, json.dumps(secret))
        await self._record(org_id, connection_id, Account(login, login, {}), None)
        return {"external_account_id": login, "label": login}

    async def register_service_account(
        self, org_id: UUID | str, connection_id: UUID | str, key_json: str
    ) -> dict[str, Any]:
        """Keep a Google Cloud service account key for an org connection, after proving it works
        (one token exchange). Only the vault ever holds the key."""
        row = await self._connection(org_id, connection_id)
        if row["status"] != "active" or row["integration"] != "google_cloud_logging" or row["scope"] != "org":
            raise VaultError("not_active", "the connection is not waiting for a service account key")
        cloud = self._provider("google_cloud_logging")
        assert isinstance(cloud, GoogleCloudProvider)
        try:
            key = cloud.parse_key(key_json)
            token = await cloud.service_account_token(key)
        except ProviderError as exc:
            code = "bad_request" if exc.code == "invalid_key" else "provider"
            raise VaultError(code, f"google cloud: {exc.message}") from exc
        await self.store_.store(org_id, connection_id, json.dumps({"kind": "gcp_service_account", **key}))
        self._service_account_tokens[str(connection_id)] = token
        account = Account(key["client_email"], key["client_email"], {"project_id": key["project_id"]})
        await self._record(org_id, connection_id, account, None)
        return {
            "external_account_id": key["client_email"],
            "label": key["client_email"],
            "project_id": key["project_id"],
        }

    # ------------------------------------------------------------------ credentials

    async def _check_use(
        self,
        org_id: UUID | str,
        row: dict[str, Any],
        task_id: UUID | str | None,
        membership_id: UUID | str | None,
    ) -> None:
        if row["status"] != "active":
            raise VaultError("not_active", f"the {row['integration']} connection is {row['status']}")
        if task_id is not None:
            async with tenant_tx(self.pool, org_id) as conn:
                task = await (
                    await conn.execute("select status, requested_by from app.tasks where id = %s", (task_id,))
                ).fetchone()
            if task is None or task[0] not in RUNNING:
                raise VaultError("forbidden", "credentials are issued only for running tasks")
            if row["scope"] == "user" and str(task[1]) != str(row["membership_id"]):
                raise VaultError("forbidden", "a linked account is used only for its owner's tasks")
        elif row["scope"] == "user" and str(membership_id or "") != str(row["membership_id"]):
            raise VaultError("forbidden", "a linked account is used only by its owner")

    async def credential(
        self,
        org_id: UUID | str,
        connection_id: UUID | str,
        task_id: UUID | str | None = None,
        membership_id: UUID | str | None = None,
    ) -> Credential:
        row = await self._connection(org_id, connection_id)
        await self._check_use(org_id, row, task_id, membership_id)
        try:
            data = _parse(await self.store_.read(org_id, connection_id))
        except SecretNotFoundError as exc:
            raise VaultError("not_found", "no secret for this connection") from exc
        base = {"connection_id": str(connection_id), "scope": row["scope"], "account_id": row["account"]}
        if data["kind"] == "static":
            return Credential(str(data["token"]), "static", **base)
        if data["kind"] == "github_installation":
            token, expires = await self._installation_token(str(connection_id), data["installation_id"])
            details = {"installation_id": data["installation_id"], "account": data["account"]}
            return Credential(token, "github_installation", expires_at=expires, details=details, **base)
        if data["kind"] == "gcp_service_account":
            token, expires = await self._service_account_token(str(connection_id), data)
            details = {"project_id": data["project_id"], "account": data["client_email"]}
            return Credential(token, "gcp_service_account", expires_at=expires, details=details, **base)
        tokens = TokenSet(
            data["access_token"], data.get("refresh_token"), data.get("expires_at"), data.get("scope", "")
        )
        if tokens.expires_at is not None and tokens.expires_at - time.time() < REFRESH_MARGIN:
            tokens, data = await self._refresh(org_id, connection_id, row["integration"])
        return Credential(
            tokens.access_token,
            "oauth2",
            expires_at=tokens.expires_at,
            details=data.get("details") or {},
            **base,
        )

    async def _refresh(
        self, org_id: UUID | str, connection_id: UUID | str, integration: str
    ) -> tuple[TokenSet, dict[str, Any]]:
        """Under a per connection lock: a concurrent caller may have refreshed already (rotating
        refresh tokens are single use, so refreshing twice would fail)."""
        async with tenant_tx(self.pool, org_id) as conn:
            await conn.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"vault-refresh:{connection_id}",)
            )
            data = _parse(await self.store_.read_in(conn, org_id, connection_id))
            tokens = TokenSet(
                data["access_token"], data.get("refresh_token"), data.get("expires_at"), data.get("scope", "")
            )
            if tokens.expires_at is not None and tokens.expires_at - time.time() >= REFRESH_MARGIN:
                return tokens, data
            try:
                fresh = await self._provider(data["provider"]).refresh(tokens)
            except ProviderError as exc:
                if exc.code in ("invalid_grant", "bad_refresh_token", "not_configured"):
                    await self._expire(org_id, connection_id, integration, exc.message)
                    raise VaultError(
                        "not_active", f"the {integration} connection expired; reconnect it"
                    ) from exc
                raise VaultError("provider", f"{integration}: {exc.message}") from exc
            data = {**data, **token_dict(fresh)}
            await self.store_.store_in(conn, org_id, connection_id, json.dumps(data))
            await conn.execute(
                "update app.connections set expires_at = to_timestamp(%s), last_refreshed_at = now() "
                "where id = %s",
                (fresh.expires_at, connection_id),
            )
        log.info("token refreshed", extra={"integration": integration})
        return fresh, data

    async def _service_account_token(self, connection_id: str, key: dict[str, Any]) -> tuple[str, float]:
        cached = self._service_account_tokens.get(connection_id)
        if cached and cached[1] - time.time() > 300:
            return cached
        cloud = self._provider("google_cloud_logging")
        assert isinstance(cloud, GoogleCloudProvider)
        try:
            minted = await cloud.service_account_token(key)
        except ProviderError as exc:
            raise VaultError("provider", f"google cloud: {exc.message}") from exc
        self._service_account_tokens[connection_id] = minted
        return minted

    async def _installation_token(self, connection_id: str, installation_id: str) -> tuple[str, float]:
        cached = self._installation_tokens.get(connection_id)
        if cached and cached[1] - time.time() > 300:
            return cached
        github = self._provider("github")
        assert isinstance(github, GitHubProvider)
        try:
            minted = await github.installation_token(installation_id)
        except ProviderError as exc:
            raise VaultError("provider", f"github: {exc.message}") from exc
        self._installation_tokens[connection_id] = minted
        return minted

    async def revoke(self, org_id: UUID | str, connection_id: UUID | str) -> None:
        """Revoke at the provider where it supports it, then delete the secret."""
        try:
            data = _parse(await self.store_.read(org_id, connection_id))
        except (SecretNotFoundError, DecryptionError):  # unreadable (a retired key): delete it all the same
            data = {}
        if data.get("kind") == "oauth2" and data.get("provider") in self.providers:
            tokens = TokenSet(data["access_token"], data.get("refresh_token"), data.get("expires_at"))
            try:
                await self.providers[data["provider"]].revoke(tokens)
            except Exception:  # the secret is deleted regardless
                log.warning("provider revocation failed", extra={"provider": data.get("provider")})
        await self.delete(org_id, connection_id)
        async with tenant_tx(self.pool, org_id) as conn:
            await conn.execute(
                "update app.connections set status = 'revoked' where id = %s and status <> 'revoked'",
                (connection_id,),
            )

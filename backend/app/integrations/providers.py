"""OAuth providers and the GitHub App: authorize URLs (built by the API with public client ids), and
code exchange, refresh, account lookup and revocation (run by the vault, which holds the client
secrets and the app key)."""

import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.settings import Settings


class ProviderError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None  # epoch seconds
    scope: str = ""

    @classmethod
    def from_response(cls, body: dict[str, Any], previous_refresh: str | None = None) -> "TokenSet":
        if "access_token" not in body:
            raise ProviderError(
                str(body.get("error", "token_error")), str(body.get("error_description", body))
            )
        expires_in = body.get("expires_in")
        return cls(
            access_token=str(body["access_token"]),
            refresh_token=str(body.get("refresh_token") or previous_refresh or "") or None,
            expires_at=time.time() + float(expires_in) if expires_in else None,
            scope=str(body.get("scope") or ""),
        )


@dataclass
class Account:
    external_id: str  # what the connection row records: a Jira cloud id or account id, a login, an email
    label: str
    details: dict[str, Any] = field(default_factory=dict)


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class OAuthProvider:
    key = ""
    uses_pkce = False
    supports_user_links = True

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=15.0)

    @property
    def configured(self) -> bool:
        raise NotImplementedError

    def authorize_url(self, state: str, redirect_uri: str, challenge: str | None, scope: str = "org") -> str:
        raise NotImplementedError

    async def exchange(self, code: str, redirect_uri: str, verifier: str | None) -> TokenSet:
        raise NotImplementedError

    async def refresh(self, tokens: TokenSet) -> TokenSet:
        raise NotImplementedError

    async def account(self, tokens: TokenSet, scope: str) -> Account:
        raise NotImplementedError

    async def revoke(self, tokens: TokenSet) -> None:
        return None

    def _secret(self, value: Any) -> str:
        if value is None:
            raise ProviderError("not_configured", f"{self.key} client secret is not configured")
        return str(value.get_secret_value())

    async def _json(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError("bad_response", f"{self.key} answered {response.status_code}") from exc


class JiraProvider(OAuthProvider):
    key = "jira"
    scopes = ("read:jira-work", "read:jira-user", "write:jira-work", "offline_access")

    @property
    def configured(self) -> bool:
        return bool(self.settings.atlassian_client_id)

    def authorize_url(self, state: str, redirect_uri: str, challenge: str | None, scope: str = "org") -> str:
        params = {
            "audience": "api.atlassian.com",
            "client_id": self.settings.atlassian_client_id or "",
            "scope": " ".join(self.scopes),
            "redirect_uri": redirect_uri,
            "state": state,
            "response_type": "code",
            "prompt": "consent",
        }
        return f"{self.settings.atlassian_authorize_url}?{urlencode(params)}"

    async def _token(self, body: dict[str, Any], previous: str | None = None) -> TokenSet:
        payload = {
            "client_id": self.settings.atlassian_client_id,
            "client_secret": self._secret(self.settings.atlassian_client_secret),
            **body,
        }
        response = await self.http.post(self.settings.atlassian_token_url, json=payload)
        return TokenSet.from_response(await self._json(response), previous)

    async def exchange(self, code: str, redirect_uri: str, verifier: str | None) -> TokenSet:
        return await self._token(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        )

    async def refresh(self, tokens: TokenSet) -> TokenSet:
        if not tokens.refresh_token:
            raise ProviderError("invalid_grant", "no refresh token")
        return await self._token({"grant_type": "refresh_token", "refresh_token": tokens.refresh_token})

    async def account(self, tokens: TokenSet, scope: str) -> Account:
        auth = {"authorization": f"Bearer {tokens.access_token}"}
        api = self.settings.atlassian_api_url
        sites = await self._json(await self.http.get(f"{api}/oauth/token/accessible-resources", headers=auth))
        if not isinstance(sites, list) or not sites:
            raise ProviderError("no_site", "The Atlassian account has no Jira site this app can access.")
        site = sites[0]
        me = await self._json(
            await self.http.get(f"{api}/ex/jira/{site['id']}/rest/api/3/myself", headers=auth)
        )
        details = {"cloud_id": site["id"], "site_url": site.get("url"), "account_id": me.get("accountId")}
        if scope == "org":
            return Account(str(site["id"]), str(site.get("url") or site["id"]), details)
        return Account(str(me.get("accountId")), str(me.get("displayName") or me.get("accountId")), details)


class GitHubProvider(OAuthProvider):
    key = "github"

    @property
    def configured(self) -> bool:
        return bool(self.settings.github_app_slug and self.settings.github_client_id)

    def install_url(self, state: str) -> str:
        query = urlencode({"state": state})
        return (
            f"{self.settings.github_web_url}/apps/{self.settings.github_app_slug}/installations/new?{query}"
        )

    def authorize_url(self, state: str, redirect_uri: str, challenge: str | None, scope: str = "org") -> str:
        if scope == "org":
            return self.install_url(state)
        params = {
            "client_id": self.settings.github_client_id or "",
            "redirect_uri": redirect_uri,
            "state": state,
        }
        return f"{self.settings.github_web_url}/login/oauth/authorize?{urlencode(params)}"

    async def _token(self, body: dict[str, Any], previous: str | None = None) -> TokenSet:
        payload = {
            "client_id": self.settings.github_client_id,
            "client_secret": self._secret(self.settings.github_client_secret),
            **body,
        }
        response = await self.http.post(
            self.settings.github_token_url,
            json=payload,
            headers={"accept": "application/json"},
        )
        return TokenSet.from_response(await self._json(response), previous)

    async def exchange(self, code: str, redirect_uri: str, verifier: str | None) -> TokenSet:
        return await self._token({"code": code, "redirect_uri": redirect_uri})

    async def refresh(self, tokens: TokenSet) -> TokenSet:
        if not tokens.refresh_token:
            raise ProviderError("invalid_grant", "no refresh token")
        return await self._token({"grant_type": "refresh_token", "refresh_token": tokens.refresh_token})

    async def account(self, tokens: TokenSet, scope: str) -> Account:
        me = await self._json(
            await self.http.get(
                f"{self.settings.github_api_url}/user", headers=self._auth(tokens.access_token)
            )
        )
        if "login" not in me:
            raise ProviderError("bad_token", "GitHub did not return the user")
        return Account(str(me["login"]), str(me["login"]), {"login": me["login"]})

    async def revoke(self, tokens: TokenSet) -> None:
        secret = self._secret(self.settings.github_client_secret)
        await self.http.request(
            "DELETE",
            f"{self.settings.github_api_url}/applications/{self.settings.github_client_id}/token",
            json={"access_token": tokens.access_token},
            auth=(self.settings.github_client_id or "", secret),
        )

    @staticmethod
    def _auth(token: str) -> dict[str, str]:
        return {
            "authorization": f"Bearer {token}",
            "accept": "application/vnd.github+json",
            "x-github-api-version": "2022-11-28",
        }

    def app_jwt(self) -> str:
        key = self.settings.github_private_key_pem
        if not key or not self.settings.github_app_id:
            raise ProviderError("not_configured", "the GitHub App id or private key is not configured")
        now = int(time.time())
        return jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": self.settings.github_app_id}, key, algorithm="RS256"
        )

    async def installation_token(self, installation_id: int | str) -> tuple[str, float]:
        response = await self.http.post(
            f"{self.settings.github_api_url}/app/installations/{installation_id}/access_tokens",
            headers=self._auth(self.app_jwt()),
        )
        body = await self._json(response)
        if response.status_code != 201 or "token" not in body:
            raise ProviderError("installation_token", str(body.get("message", response.status_code)))
        expires = datetime.fromisoformat(str(body["expires_at"]).replace("Z", "+00:00")).timestamp()
        return str(body["token"]), expires

    async def user_installations(self, user_token: str) -> list[dict[str, Any]]:
        body = await self._json(
            await self.http.get(
                f"{self.settings.github_api_url}/user/installations", headers=self._auth(user_token)
            )
        )
        return list(body.get("installations") or [])


class GoogleProvider(OAuthProvider):
    key = "google_workspace"
    uses_pkce = True
    scopes = (
        "openid",
        "email",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/spreadsheets",
    )

    @property
    def configured(self) -> bool:
        return bool(self.settings.google_client_id)

    def authorize_url(self, state: str, redirect_uri: str, challenge: str | None, scope: str = "org") -> str:
        params = {
            "client_id": self.settings.google_client_id or "",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            "code_challenge": challenge or "",
            "code_challenge_method": "S256",
        }
        return f"{self.settings.google_auth_url}?{urlencode(params)}"

    async def _token(self, body: dict[str, Any], previous: str | None = None) -> TokenSet:
        form = {
            "client_id": self.settings.google_client_id,
            "client_secret": self._secret(self.settings.google_client_secret),
            **body,
        }
        response = await self.http.post(self.settings.google_token_url, data=form)
        return TokenSet.from_response(await self._json(response), previous)

    async def exchange(self, code: str, redirect_uri: str, verifier: str | None) -> TokenSet:
        body = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
        if verifier:
            body["code_verifier"] = verifier
        return await self._token(body)

    async def refresh(self, tokens: TokenSet) -> TokenSet:
        if not tokens.refresh_token:
            raise ProviderError("invalid_grant", "no refresh token")
        body = {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token}
        return await self._token(body, previous=tokens.refresh_token)

    async def account(self, tokens: TokenSet, scope: str) -> Account:
        me = await self._json(
            await self.http.get(
                self.settings.google_userinfo_url, headers={"authorization": f"Bearer {tokens.access_token}"}
            )
        )
        if "email" not in me:
            raise ProviderError("bad_token", "Google did not return the account")
        return Account(str(me["email"]), str(me["email"]), {"email": me["email"]})

    async def revoke(self, tokens: TokenSet) -> None:
        await self.http.post(
            self.settings.google_revoke_url, params={"token": tokens.refresh_token or tokens.access_token}
        )


GCP_SCOPES = ("https://www.googleapis.com/auth/cloud-platform.read-only",)


class GoogleCloudProvider(OAuthProvider):
    """Google Cloud with a service account key an admin uploads (no browser flow): the vault keeps
    the key, signs a JWT assertion and exchanges it for a one hour token. The token endpoint is
    always ours to choose (settings), never the key file's ``token_uri``."""

    key = "google_cloud_logging"
    supports_user_links = False

    @property
    def configured(self) -> bool:
        return True

    @staticmethod
    def parse_key(text: str) -> dict[str, str]:
        """The parts of a service account key file we keep; refuses anything else."""
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise ProviderError("invalid_key", "not a JSON service account key") from exc
        if not isinstance(data, dict) or data.get("type") != "service_account":
            raise ProviderError("invalid_key", "not a service account key file")
        email, project, key = data.get("client_email"), data.get("project_id"), data.get("private_key")
        if not (isinstance(email, str) and email.endswith(".gserviceaccount.com") and "@" in email):
            raise ProviderError("invalid_key", "the key has no service account email")
        if not (isinstance(project, str) and re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", project)):
            raise ProviderError("invalid_key", "the key has no valid project id")
        try:
            loaded = serialization.load_pem_private_key(str(key).encode(), password=None)
        except (ValueError, TypeError) as exc:
            raise ProviderError("invalid_key", "the key's private key cannot be read") from exc
        if not isinstance(loaded, rsa.RSAPrivateKey):
            raise ProviderError("invalid_key", "the key is not an RSA key")
        return {"client_email": email, "project_id": project, "private_key": str(key),
                "private_key_id": str(data.get("private_key_id") or "")}  # fmt: skip

    async def service_account_token(self, key: dict[str, str]) -> tuple[str, float]:
        now = int(time.time())
        claims = {
            "iss": key["client_email"],
            "scope": " ".join(GCP_SCOPES),
            "aud": self.settings.gcp_token_url,
            "iat": now,
            "exp": now + 3600,
        }
        headers = {"kid": key["private_key_id"]} if key.get("private_key_id") else None
        assertion = jwt.encode(claims, key["private_key"], algorithm="RS256", headers=headers)
        try:
            response = await self.http.post(
                self.settings.gcp_token_url,
                data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
            )
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError("unavailable", f"token endpoint: {type(exc).__name__}") from exc
        if response.status_code != 200 or "access_token" not in body:
            reason = str(body.get("error_description") or body.get("error") or response.status_code)
            raise ProviderError(str(body.get("error") or "token_error"), reason[:200])
        return str(body["access_token"]), time.time() + float(body.get("expires_in") or 3600)


def providers(settings: Settings, http: httpx.AsyncClient | None = None) -> dict[str, OAuthProvider]:
    client = http or httpx.AsyncClient(timeout=15.0)
    return {
        p.key: p
        for p in (
            JiraProvider(settings, client),
            GitHubProvider(settings, client),
            GoogleProvider(settings, client),
            GoogleCloudProvider(settings, client),
        )
    }


def token_dict(tokens: TokenSet) -> dict[str, Any]:
    return asdict(tokens)

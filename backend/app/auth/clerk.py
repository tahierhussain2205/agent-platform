"""Clerk session token verification (spec 14.3).

- RS256 only, key chosen by ``kid`` from the instance JWKS. Keys are cached; an unknown ``kid``
  triggers a refresh, at most once per ``refresh_interval`` (key rotation without hammering Clerk).
- ``iss`` must equal the configured issuer; ``exp``/``nbf`` checked with a small leeway.
- ``azp`` (the browser origin that obtained the token) must be one of the console origins.
- Sessions with status ``pending`` (``sts``) are refused: the user has not finished setup (for
  example, has not chosen or created an org).
- The active org comes from the v2 ``o.id`` claim, falling back to v1's ``org_id``.

The org role in the token is informational only: privilege comes from our database.
"""

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm


class AuthError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class VerifiedSession:
    clerk_user_id: str
    session_id: str
    clerk_org_id: str | None
    org_role: str | None
    claims: dict[str, Any] = field(repr=False)


class ClerkVerifier:
    def __init__(
        self,
        issuer: str,
        jwks_url: str,
        authorized_parties: list[str],
        http: httpx.AsyncClient | None = None,
        jwks_bearer: str | None = None,
        leeway: int = 5,
        refresh_interval: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.issuer = issuer
        self.jwks_url = jwks_url
        self.authorized_parties = set(authorized_parties)
        self._http = http or httpx.AsyncClient(timeout=5.0)
        self._jwks_bearer = jwks_bearer  # Backend API JWKS (/v1/jwks) needs the secret key
        self.leeway = leeway
        self.refresh_interval = refresh_interval
        self._clock = clock
        self._keys: dict[str, Any] = {}
        self._last_refresh: float | None = None
        self._lock = asyncio.Lock()

    async def _refresh(self) -> None:
        headers = {"Authorization": f"Bearer {self._jwks_bearer}"} if self._jwks_bearer else {}
        try:
            response = await self._http.get(self.jwks_url, headers=headers)
            response.raise_for_status()
            jwks = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AuthError(503, "auth_unavailable", "Could not verify the session right now.") from exc
        finally:
            self._last_refresh = self._clock()
        keys: dict[str, Any] = {}
        for jwk in jwks.get("keys", []):
            if jwk.get("kty") == "RSA" and jwk.get("kid") and jwk.get("use", "sig") == "sig":
                keys[jwk["kid"]] = RSAAlgorithm.from_jwk(json.dumps(jwk))
        self._keys = keys

    async def _key(self, kid: str) -> Any:
        if kid in self._keys:
            return self._keys[kid]
        async with self._lock:
            if kid not in self._keys:
                recently = (
                    self._last_refresh is not None
                    and self._clock() - self._last_refresh < self.refresh_interval
                )
                if not (recently and self._keys):
                    await self._refresh()
        if kid not in self._keys:
            raise AuthError(401, "invalid_token", "The session token is not valid.")
        return self._keys[kid]

    async def verify(self, token: str) -> VerifiedSession:
        invalid = AuthError(401, "invalid_token", "The session token is not valid.")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise invalid from exc
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise invalid
        key = await self._key(header["kid"])
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self.issuer,
                leeway=self.leeway,
                options={"require": ["exp", "nbf", "iat", "iss", "sub", "sid"], "verify_aud": False},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError(401, "token_expired", "The session has expired. Sign in again.") from exc
        except jwt.PyJWTError as exc:
            raise invalid from exc

        if claims.get("azp") not in self.authorized_parties:
            raise invalid
        if claims.get("sts") == "pending":
            raise AuthError(403, "session_pending", "Finish setting up your account first.")

        org = claims.get("o") if isinstance(claims.get("o"), dict) else None
        if org is not None:
            org_id, role = org.get("id"), org.get("rol")
        else:
            org_id, role = claims.get("org_id"), claims.get("org_role")
        return VerifiedSession(
            clerk_user_id=str(claims["sub"]),
            session_id=str(claims["sid"]),
            clerk_org_id=str(org_id) if org_id else None,
            org_role=str(role) if role else None,
            claims=claims,
        )

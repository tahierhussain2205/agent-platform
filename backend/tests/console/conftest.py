"""Console test fixtures: signing keys and tokens, a mock JWKS endpoint, a mock Clerk Backend API,
and an app wired to a private world (these tests commit changes)."""

import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
import psycopg
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from psycopg import sql

from app import kv
from app.api.main import Resources, create_app
from app.auth.clerk import ClerkVerifier
from app.db.pool import open_pool
from app.db.roles import LoginUser, ensure_login_user
from app.identity.clerk_api import ClerkClient
from app.settings import Settings
from tests.conftest import ADMIN_URL
from tests.stack import FakeTemporal
from tests.world import World

ISSUER = "https://clerk.test.example"
ORIGIN = "http://localhost:5173"
PASSWORD = "console-test-password-0001"


class SigningKey:
    def __init__(self, kid: str | None = None) -> None:
        self.kid = kid or f"kid_{uuid.uuid4().hex[:8]}"
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(RSAAlgorithm.to_jwk(self.private.public_key()))
        self.jwk = {**jwk, "kid": self.kid, "use": "sig", "alg": "RS256"}


@dataclass
class Jwks:
    """Mock JWKS endpoint: the keys it serves can change (rotation) or it can go down."""

    keys: list[SigningKey]
    fetches: int = 0
    down: bool = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.fetches += 1
        if self.down:
            return httpx.Response(503)
        return httpx.Response(200, json={"keys": [k.jwk for k in self.keys]})


def mint(
    key: SigningKey,
    sub: str,
    org: str | None,
    *,
    rol: str = "member",
    sts: str | None = "active",
    azp: str | None = ORIGIN,
    iss: str = ISSUER,
    exp_in: int = 60,
    nbf_in: int = 0,
    version: int = 2,
    drop: tuple[str, ...] = (),
    extra: dict[str, Any] | None = None,
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": sub,
        "sid": f"sess_{uuid.uuid4().hex[:8]}",
        "iss": iss,
        "iat": now,
        "nbf": now + nbf_in,
        "exp": now + exp_in,
        "v": version,
    }
    if azp is not None:
        claims["azp"] = azp
    if sts is not None:
        claims["sts"] = sts
    if org is not None:
        if version == 2:
            claims["o"] = {"id": org, "rol": rol, "slg": org.removeprefix("org_")}
        else:
            claims.update({"org_id": org, "org_role": f"org:{rol}"})
    claims.update(extra or {})
    for name in drop:
        claims.pop(name, None)
    return jwt.encode(claims, key.private, algorithm="RS256", headers={"kid": key.kid})


@dataclass
class ClerkBackend:
    """Mock Clerk Backend API: records calls; failures can be injected per method and path."""

    calls: list[tuple[str, str, Any]] = field(default_factory=list)
    invitations: dict[str, dict[str, Any]] = field(default_factory=dict)
    failures: dict[tuple[str, str], tuple[int, str]] = field(default_factory=dict)

    def fail(self, method: str, path_pattern: str, status: int, code: str = "error") -> None:
        self.failures[(method, path_pattern)] = (status, code)

    def paths(self, method: str | None = None) -> list[str]:
        return [p for m, p, _ in self.calls if method is None or m == method]

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk_test_console"
        path = request.url.path.removeprefix("/v1")
        body: Any = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        for (method, pattern), (status, code) in self.failures.items():
            if method == request.method and re.fullmatch(pattern, path):
                return httpx.Response(
                    status,
                    json={"errors": [{"code": code, "message": code, "long_message": f"{code} (mock)"}]},
                )
        if m := re.fullmatch(r"/organizations/([^/]+)/invitations", path):
            if request.method == "POST":
                inv = {
                    "id": f"orginv_{uuid.uuid4().hex[:8]}",
                    "email_address": body["email_address"],
                    "role": body["role"],
                    "status": "pending",
                    "organization_id": m.group(1),
                }
                self.invitations[inv["id"]] = inv
                return httpx.Response(200, json=inv)
            pending = [
                i
                for i in self.invitations.values()
                if i["organization_id"] == m.group(1) and i["status"] == "pending"
            ]
            return httpx.Response(200, json={"data": pending, "total_count": len(pending)})
        if m := re.fullmatch(r"/organizations/[^/]+/invitations/([^/]+)/revoke", path):
            self.invitations[m.group(1)]["status"] = "revoked"
            return httpx.Response(200, json=self.invitations[m.group(1)])
        return httpx.Response(200, json={"object": "ok"})


def _login_user(world: World, component: str, role: str) -> LoginUser:
    user = LoginUser(component, f"t{uuid.uuid4().hex[:6]}_{component}", role)  # type: ignore[arg-type]
    with psycopg.connect(world.url, autocommit=True) as conn:
        ensure_login_user(conn, user, PASSWORD)
    return user


def dsn(world: World, user: LoginUser) -> str:
    base, _, dbname = world.url.rpartition("/")
    return f"postgresql://{user.username}:{PASSWORD}@{base.rsplit('@', 1)[1]}/{dbname}"


@pytest.fixture(scope="module")
def login_users(module_world: World) -> Iterator[dict[str, LoginUser]]:
    users = {
        "api": _login_user(module_world, "api", "app_user"),
        "identity": _login_user(module_world, "identity", "app_identity_sync"),
    }
    yield users
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        for user in users.values():
            conn.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(user.username)))


@pytest.fixture
def signing_key() -> SigningKey:
    return SigningKey()


@pytest.fixture
def jwks(signing_key: SigningKey) -> Jwks:
    return Jwks([signing_key])


@pytest.fixture
def clerk_backend() -> ClerkBackend:
    return ClerkBackend()


@pytest.fixture(scope="module")
def temporal() -> FakeTemporal:
    """Records the workflows the console starts (follow ups such as a member's removal)."""
    return FakeTemporal()


@pytest_asyncio.fixture(loop_scope="session")
async def console(
    module_world: World,
    login_users: dict[str, LoginUser],
    jwks: Jwks,
    clerk_backend: ClerkBackend,
    temporal: FakeTemporal,
) -> AsyncIterator[httpx.AsyncClient]:
    api_pool = await open_pool(dsn(module_world, login_users["api"]), "api", min_size=0, max_size=4)
    identity_pool = await open_pool(
        dsn(module_world, login_users["identity"]), "identity", min_size=0, max_size=2
    )
    kv_client: Any = kv.make_client("redis://localhost:56379/14")
    try:
        await kv_client.ping()
    except Exception:
        kv_client = None
    resources = Resources(
        db_pool=api_pool,
        identity_pool=identity_pool,
        kv=kv_client,
        temporal=temporal,
        clerk_verifier=ClerkVerifier(
            ISSUER,
            "https://clerk.test.example/.well-known/jwks.json",
            [ORIGIN],
            http=httpx.AsyncClient(transport=httpx.MockTransport(jwks.handler)),
        ),
        clerk_client=ClerkClient(
            "https://api.clerk.test/v1",
            "sk_test_console",
            http=httpx.AsyncClient(transport=httpx.MockTransport(clerk_backend.handler)),
        ),
    )
    app = create_app(Settings(env="test", console_origins=ORIGIN), resources)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://api"
    ) as client:
        yield client
    await api_pool.close()
    await identity_pool.close()
    if kv_client is not None:
        await kv_client.aclose()


def auth(key: SigningKey, sub: str, org: str | None, **kwargs: Any) -> dict[str, str]:
    return {"authorization": f"Bearer {mint(key, sub, org, **kwargs)}", "origin": ORIGIN}


def q(world: World, query: str, params: tuple[Any, ...] = ()) -> Any:
    with psycopg.connect(world.url) as conn:
        row = conn.execute(query, params).fetchone()
        return row[0] if row and len(row) == 1 else row

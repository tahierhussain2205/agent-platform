"""Shared integration fixtures (a pytest plugin): login users for every component on the module's
world database, their pools, Key Value, the vault, a fake Temporal client, the Slack client
pointed at the Slack stand-in (fakes service), and an API app wired to them."""

import base64
import os
import socket
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from temporalio.exceptions import WorkflowAlreadyStartedError

from app import kv
from app.api.main import Resources, create_app
from app.crypto import Envelope, LocalKeyProvider
from app.db.pool import open_pool
from app.db.roles import Component, LoginUser, ensure_login_user
from app.settings import Settings
from app.slack.client import SlackClient
from app.slack.limiter import SlackRateLimiter
from app.vault import Vault
from tests.conftest import ADMIN_URL
from tests.world import World

PASSWORD = "slack-test-password-00001"
SIGNING_SECRET = "local-slack-signing-secret"
FAKES = os.environ.get("TEST_FAKES_URL", "http://localhost:4100")
KEY = base64.b64encode(b"slack-tests-encryption-key-00001").decode()
ENVELOPE = Envelope(LocalKeyProvider.from_config(f"v1:{KEY}", "v1"))


def fakes_up() -> bool:
    host, port = FAKES.split("//")[1].split(":")
    try:
        socket.create_connection((host, int(port)), timeout=1).close()
        return True
    except OSError:
        return False


@dataclass
class FakeTemporal:
    started: list[tuple[str, Any, str]] = field(default_factory=list)
    fail: bool = False

    async def start_workflow(self, workflow: str, arg: Any, *, id: str, task_queue: str) -> None:
        if self.fail:
            raise RuntimeError("temporal unavailable")
        if any(existing == id for _, _, existing in self.started):
            raise WorkflowAlreadyStartedError(id, workflow)
        self.started.append((workflow, arg, id))


@dataclass
class Env:
    world: World
    users: dict[Component, LoginUser]
    dsn: dict[str, str]


@pytest.fixture(scope="module")
def env(module_world: World) -> Iterator[Env]:
    tag = uuid.uuid4().hex[:6]
    roles: dict[Component, str] = {
        "api": "app_user",
        "worker": "app_user",
        "vault": "app_vault",
        "identity": "app_identity_sync",
    }
    users = {c: LoginUser(c, f"t{tag}_{c}", r) for c, r in roles.items()}
    base, _, dbname = module_world.url.rpartition("/")
    host = base.rsplit("@", 1)[1]
    with psycopg.connect(module_world.url, autocommit=True) as conn:
        for user in users.values():
            ensure_login_user(conn, user, PASSWORD)
    yield Env(
        module_world,
        users,
        {c: f"postgresql://{u.username}:{PASSWORD}@{host}/{dbname}" for c, u in users.items()},
    )
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        for user in users.values():
            conn.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(user.username)))


@dataclass
class Stack:
    env: Env
    pool: Any
    vault_pool: Any
    identity_pool: Any
    vault: Vault
    kv: Any
    slack: SlackClient
    temporal: FakeTemporal


@pytest_asyncio.fixture(loop_scope="session")
async def stack(env: Env) -> AsyncIterator[Stack]:
    pool = await open_pool(env.dsn["api"], "api", min_size=0, max_size=4)
    vault_pool = await open_pool(env.dsn["vault"], "vault", min_size=0, max_size=2)
    identity_pool = await open_pool(env.dsn["identity"], "identity", min_size=0, max_size=2)
    kv_client: Any = kv.make_client("redis://localhost:56379/13")
    try:
        await kv_client.ping()
    except Exception:
        kv_client = None
    slack = SlackClient(f"{FAKES}/slack/api", limiter=SlackRateLimiter(kv_client) if kv_client else None)
    yield Stack(
        env, pool, vault_pool, identity_pool, Vault(vault_pool, ENVELOPE), kv_client, slack, FakeTemporal()
    )
    await slack.aclose()
    for p in (pool, vault_pool, identity_pool):
        await p.close()
    if kv_client is not None:
        await kv_client.aclose()


def app_for(stack: Stack, **overrides: Any) -> Any:
    options: dict[str, Any] = {
        "db_pool": stack.pool, "identity_pool": stack.identity_pool, "vault_pool": stack.vault_pool,
        "vault": stack.vault, "kv": stack.kv, "slack": stack.slack, "temporal": stack.temporal,
    }  # fmt: skip
    return create_app(Settings(env="test"), Resources(**{**options, **overrides}))


@pytest_asyncio.fixture(loop_scope="session")
async def api(stack: Stack) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(stack), raise_app_exceptions=False), base_url="http://api"
    ) as client:
        yield client


async def seed_slack_token(stack: Stack, org_id: Any, team_id: str, token: str | None = None) -> Any:
    """An active Slack connection with its bot token in the vault (what an install leaves behind)."""
    with psycopg.connect(stack.env.world.url) as conn:
        conn.execute(
            "insert into app.org_integrations (org_id, integration_key, kind, enabled) values (%s, 'slack', 'native', true) "
            "on conflict (org_id, integration_key) do update set enabled = true",
            (org_id,),
        )
        conn.execute(
            "update app.connections set status = 'revoked' where org_id = %s and integration_key = 'slack' and status = 'active'",
            (org_id,),
        )
        row = conn.execute(
            "insert into app.connections (org_id, integration_key, scope, external_account_id) values (%s, 'slack', 'org', %s) returning id",
            (org_id, team_id),
        ).fetchone()
        conn.commit()
    assert row is not None
    await stack.vault.store(org_id, row[0], token or f"xoxb-local-{team_id}")
    return row[0]

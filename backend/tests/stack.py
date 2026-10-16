"""Shared integration fixtures (a pytest plugin): login users for every component on the module's
world database, their pools, Key Value, the vault, a fake Temporal client, the Slack client
pointed at the Slack stand-in (fakes service), and an API app wired to them."""

import base64
import os
import socket
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import WorkflowAlreadyStartedError

from app import kv
from app.api.main import Resources, create_app
from app.crypto import Envelope, LocalKeyProvider
from app.db.pool import open_pool
from app.db.roles import Component, LoginUser, ensure_login_user
from app.integrations.providers import providers
from app.settings import Settings
from app.slack.client import SlackClient
from app.slack.limiter import SlackRateLimiter
from app.vault_service import LocalVault
from tests.conftest import ADMIN_URL
from tests.world import World

PASSWORD = "slack-test-password-00001"
SIGNING_SECRET = "local-slack-signing-secret"
FAKES = os.environ.get("TEST_FAKES_URL", "http://localhost:4100")
KEY = base64.b64encode(b"slack-tests-encryption-key-00001").decode()
ENVELOPE = Envelope(LocalKeyProvider.from_config(f"v1:{KEY}", "v1"))
GITHUB_APP_KEY = (
    Path(__file__).resolve().parents[2] / "dev" / "github" / "app-dev-private-key.pem"
).read_text()
# Provider settings pointing at the stand-ins (the vault's view of Jira, GitHub and Google).
PROVIDER_SETTINGS = Settings(
    atlassian_authorize_url=f"{FAKES}/atlassian/authorize",
    atlassian_token_url=f"{FAKES}/atlassian/oauth/token",
    atlassian_api_url=f"{FAKES}/atlassian/api",
    atlassian_client_id="local-jira-client",
    atlassian_client_secret="local-jira-client-secret",
    github_web_url=f"{FAKES}/github",
    github_token_url=f"{FAKES}/github/login/oauth/access_token",
    github_api_url=f"{FAKES}/github/api",
    github_app_id="424242",
    github_app_slug="agent-platform-local",
    github_app_private_key=GITHUB_APP_KEY,
    github_client_id="Iv1.localclient",
    github_client_secret="local-github-client-secret",
    google_auth_url=f"{FAKES}/google/o/oauth2/v2/auth",
    google_token_url=f"{FAKES}/google/token",
    google_revoke_url=f"{FAKES}/google/revoke",
    google_userinfo_url=f"{FAKES}/google/userinfo",
    google_mcp_url=f"{FAKES}/google/mcp",
    google_client_id="local-google-client",
    google_client_secret="local-google-client-secret",
    gcp_token_url=f"{FAKES}/gcp/token",
    gcp_logging_url=f"{FAKES}/gcp/logging",
    gcp_error_reporting_url=f"{FAKES}/gcp/errorreporting",
    gcp_trace_url=f"{FAKES}/gcp/trace",
    sandbox_url=f"{FAKES}/sandbox/v1",
    sandbox_api_key="sbx-local-key",
)
GCP_KEY = (Path(__file__).resolve().parents[2] / "dev" / "gcp" / "service-account-dev.json").read_text()


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
    results: dict[str, Any] = field(default_factory=dict)  # workflow id -> result, once completed

    async def start_workflow(
        self, workflow: str, arg: Any = None, *, id: str, task_queue: str, args: Any = None, **_: Any
    ) -> None:
        if self.fail:
            raise RuntimeError("temporal unavailable")
        if id in self.results:  # a closed workflow's id may be used again
            del self.results[id]
            self.started = [s for s in self.started if s[2] != id]
        if any(existing == id for _, _, existing in self.started):
            raise WorkflowAlreadyStartedError(id, workflow)
        self.started.append((workflow, arg if args is None else list(args), id))

    def get_workflow_handle(self, id: str) -> "FakeHandle":
        return FakeHandle(self, id)


@dataclass
class FakeHandle:
    temporal: FakeTemporal
    id: str

    async def describe(self) -> Any:
        if not any(existing == self.id for _, _, existing in self.temporal.started):
            raise RuntimeError("workflow not found")
        done = self.id in self.temporal.results
        status = WorkflowExecutionStatus.COMPLETED if done else WorkflowExecutionStatus.RUNNING
        return SimpleNamespace(status=status, close_time=None)

    async def result(self) -> Any:
        return self.temporal.results[self.id]


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
    vault: LocalVault
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
    stack_vault = LocalVault(vault_pool, ENVELOPE, providers(PROVIDER_SETTINGS))
    slack = SlackClient(f"{FAKES}/slack/api", limiter=SlackRateLimiter(kv_client) if kv_client else None)
    yield Stack(
        env,
        pool,
        vault_pool,
        identity_pool,
        stack_vault,
        kv_client,
        slack,
        FakeTemporal(),
    )
    await slack.aclose()
    await stack_vault.aclose()
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

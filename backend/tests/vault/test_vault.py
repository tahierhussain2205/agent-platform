"""The token vault: exchange, refresh, GitHub installations, task scoping, revocation, isolation, and
the HTTP service with its client. Runs against the Jira, GitHub and Google stand-ins."""

import asyncio
import json
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import psycopg
import pytest

from app.integrations.providers import GoogleProvider, pkce_pair
from app.settings import Settings
from app.vault_service import VaultError
from app.vault_service.app import create_vault_app
from app.vault_service.client import RemoteVault
from tests.stack import FAKES, PROVIDER_SETTINGS, Env, Stack, fakes_up

pytestmark = pytest.mark.skipif(not fakes_up(), reason="stand-ins not reachable (run `make infra`)")
REDIRECT = "http://api.test/oauth/callback"
SETUP = "http://localhost:8000/oauth/github/install"  # the GitHub App's setup URL in the stand-in


def sql(env: Env, statement: str, params: tuple[Any, ...] = ()) -> Any:
    with psycopg.connect(env.world.url) as conn:
        cur = conn.execute(statement, params)
        rows = cur.fetchall() if cur.description else None
        conn.commit()
        return rows


@pytest.fixture(scope="module", autouse=True)
def integrations(env: Env) -> None:
    for org in (env.world.acme, env.world.globex):
        for key, kind in (("jira", "native"), ("github", "native"), ("google_workspace", "vendor_mcp")):
            sql(
                env,
                "insert into app.org_integrations (org_id, integration_key, kind, enabled) values (%s, %s, %s, true) "
                "on conflict (org_id, integration_key) do update set enabled = true, health = 'unknown'",
                (org.id, key, kind),
            )


def connection(env: Env, org: Any, key: str, member: Any = None) -> uuid.UUID:
    scope = "user" if member else "org"
    if member:
        sql(env, "update app.connections set status = 'revoked' where org_id = %s and integration_key = %s "
                 "and membership_id = %s and status = 'active'", (org.id, key, member))  # fmt: skip
    else:
        sql(env, "update app.connections set status = 'revoked' where org_id = %s and integration_key = %s "
                 "and scope = 'org' and status = 'active'", (org.id, key))  # fmt: skip
    row = sql(
        env,
        "insert into app.connections (org_id, integration_key, scope, membership_id) values (%s, %s, %s, %s) returning id",
        (org.id, key, scope, member),
    )
    return row[0][0]  # type: ignore[no-any-return]


async def consent(path: str, account: str, **fields: str) -> dict[str, str]:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{FAKES}{path}", data={**fields, "account": account, "decision": "allow"}
        )
    assert response.status_code == 302, response.text
    return {k: v[0] for k, v in parse_qs(urlparse(response.headers["location"]).query).items()}


async def jira_code(account: str) -> str:
    fields = {"redirect_uri": REDIRECT, "state": "s", "scope": "read:jira-work"}
    return (await consent("/atlassian/authorize", account, **fields))["code"]


def task(env: Env, org: Any, member: Any, status: str = "running") -> uuid.UUID:
    row = sql(
        env,
        "insert into app.tasks (org_id, requested_by, trigger, model_tier, status, started_at, finished_at) "
        "values (%s, %s, 'mention', 'standard', %s, now(), case when %s in ('completed', 'failed', 'cancelled') "
        "then now() end) returning id",
        (org.id, member, status, status),
    )
    return row[0][0]  # type: ignore[no-any-return]


async def _get(url: str, headers: dict[str, str]) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.get(url, headers=headers)


# ------------------------------------------------------------------ OAuth connections


async def test_jira_exchange_credential_and_api_call(stack: Stack, env: Env) -> None:
    acme = env.world.acme
    conn = connection(env, acme, "jira")
    result = await stack.vault.exchange(acme.id, conn, "jira", await jira_code("acc-alice"), REDIRECT)
    assert result == {"external_account_id": "cloud-acme", "label": "https://acme.atlassian.net"}
    row = sql(env, "select external_account_id, external_account_label, granted_scopes, expires_at > now(), "
                   "last_refreshed_at is not null from app.connections where id = %s", (conn,))[0]  # fmt: skip
    assert row == ("cloud-acme", "https://acme.atlassian.net", ["read:jira-work"], True, True)
    stored = sql(env, "select ciphertext from app.connection_secrets where connection_id = %s", (conn,))[0][0]
    assert b"atl_" not in bytes(stored)  # encrypted at rest
    cred = await stack.vault.credential(acme.id, conn)
    assert (cred.kind, cred.scope, cred.details["cloud_id"]) == ("oauth2", "org", "cloud-acme")
    me = await _get(
        f"{FAKES}/atlassian/api/ex/jira/cloud-acme/rest/api/3/myself",
        headers={"authorization": f"Bearer {cred.token}"},
    )
    assert me.json()["accountId"] == "acc-alice"
    # Token sets never leave the vault through a raw read.
    with pytest.raises(VaultError) as info:
        await stack.vault.read(acme.id, conn)
    assert info.value.code == "forbidden"


async def test_refresh_near_expiry_rotates_once_even_when_concurrent(stack: Stack, env: Env) -> None:
    acme = env.world.acme
    conn = connection(env, acme, "jira")
    await stack.vault.exchange(acme.id, conn, "jira", await jira_code("acc-alice"), REDIRECT)
    first = await stack.vault.credential(acme.id, conn)
    await _age(stack, acme.id, conn)
    issued = await asyncio.gather(*(stack.vault.credential(acme.id, conn) for _ in range(5)))
    tokens = {c.token for c in issued}
    assert len(tokens) == 1  # one refresh, shared: a second refresh would have reused a rotated token
    assert first.token not in tokens
    me = await _get(
        f"{FAKES}/atlassian/api/ex/jira/cloud-acme/rest/api/3/myself",
        headers={"authorization": f"Bearer {tokens.pop()}"},
    )
    assert me.status_code == 200


async def _age(stack: Stack, org: Any, conn: Any, refresh_token: str | None = None) -> None:
    """Make the stored access token look expired (and optionally swap the refresh token)."""
    raw = await stack.vault.store_.read(org, conn)
    data = json.loads(raw)
    data["expires_at"] = time.time() - 10
    if refresh_token is not None:
        data["refresh_token"] = refresh_token
    await stack.vault.store_.store(org, conn, json.dumps(data))


async def test_a_dead_refresh_token_expires_the_connection(stack: Stack, env: Env) -> None:
    acme = env.world.acme
    conn = connection(env, acme, "jira")
    await stack.vault.exchange(acme.id, conn, "jira", await jira_code("acc-alice"), REDIRECT)
    await _age(stack, acme.id, conn, refresh_token="atl_r_revoked")
    with pytest.raises(VaultError) as info:
        await stack.vault.credential(acme.id, conn)
    assert info.value.code == "not_active"
    assert "reconnect" in info.value.message
    assert sql(env, "select status from app.connections where id = %s", (conn,)) == [("expired",)]
    assert sql(
        env,
        "select health from app.org_integrations where org_id = %s and integration_key = 'jira'",
        (acme.id,),
    ) == [("broken",)]
    with pytest.raises(VaultError):
        await stack.vault.credential(acme.id, conn)


async def test_google_exchange_uses_pkce(stack: Stack, env: Env) -> None:
    acme = env.world.acme
    verifier, challenge = pkce_pair()
    url = GoogleProvider(PROVIDER_SETTINGS).authorize_url("st", REDIRECT, challenge)
    params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert (params["access_type"], params["code_challenge_method"]) == ("offline", "S256")
    fields = {
        k: params[k] for k in ("redirect_uri", "state", "scope", "code_challenge", "code_challenge_method")
    }
    code = (await consent("/google/o/oauth2/v2/auth", "alice@acme.test", **fields))["code"]
    conn = connection(env, acme, "google_workspace")
    with pytest.raises(VaultError):  # the wrong verifier is refused by the provider
        await stack.vault.exchange(acme.id, conn, "google_workspace", code, REDIRECT, "wrong-verifier")
    code = (await consent("/google/o/oauth2/v2/auth", "alice@acme.test", **fields))["code"]
    result = await stack.vault.exchange(acme.id, conn, "google_workspace", code, REDIRECT, verifier)
    assert result["external_account_id"] == "alice@acme.test"


# ------------------------------------------------------------------ GitHub App


async def test_github_installation_is_registered_only_by_someone_who_can_access_it(
    stack: Stack, env: Env
) -> None:
    acme = env.world.acme
    setup = await consent("/github/apps/agent-platform-local/installations/new", "alice-gh|acme", state="st")
    conn = connection(env, acme, "github")
    result = await stack.vault.register_installation(
        acme.id, conn, setup["installation_id"], setup["code"], SETUP
    )
    assert result["external_account_id"] == "acme"
    cred = await stack.vault.credential(acme.id, conn)
    assert cred.kind == "github_installation"
    assert cred.token.startswith("ghs_")
    auth = {"authorization": f"Bearer {cred.token}"}
    repos = (await _get(f"{FAKES}/github/api/installation/repositories", headers=auth)).json()
    assert "acme/cart-service" in [r["full_name"] for r in repos["repositories"]]
    # Dave installs on globex; Alice tries to claim that installation for Acme.
    globex_install = await consent(
        "/github/apps/agent-platform-local/installations/new", "dave-gh|globex", state="x"
    )
    install_page = "/github/apps/agent-platform-local/installations/new"
    alice_code = (await consent(install_page, "alice-gh|acme", state="y"))["code"]
    other = connection(env, acme, "github")
    with pytest.raises(VaultError) as info:
        await stack.vault.register_installation(
            acme.id, other, globex_install["installation_id"], alice_code, SETUP
        )
    assert info.value.code == "forbidden"


# ------------------------------------------------------------------ user links and task scoping


async def test_user_links_are_used_only_for_their_owner(stack: Stack, env: Env) -> None:
    acme = env.world.acme
    bob, alice = acme.user, acme.admin
    link = connection(env, acme, "jira", bob)
    result = await stack.vault.exchange(acme.id, link, "jira", await jira_code("acc-bob"), REDIRECT)
    assert result["external_account_id"] == "acc-bob"
    assert (await stack.vault.credential(acme.id, link, task(env, acme, bob))).scope == "user"
    assert (await stack.vault.credential(acme.id, link, membership_id=bob)).account_id == "acc-bob"
    for kwargs in (
        {"task_id": task(env, acme, alice)},  # someone else's task
        {"task_id": task(env, acme, bob, "completed")},  # not running
        {"membership_id": alice},  # someone else
        {},  # nobody
    ):
        with pytest.raises(VaultError) as info:
            await stack.vault.credential(acme.id, link, **kwargs)
        assert info.value.code == "forbidden"


async def test_a_jira_link_must_be_on_the_orgs_site(stack: Stack, env: Env) -> None:
    acme = env.world.acme
    org_conn = connection(env, acme, "jira")
    await stack.vault.exchange(acme.id, org_conn, "jira", await jira_code("acc-alice"), REDIRECT)
    link = connection(env, acme, "jira", acme.user)
    with pytest.raises(VaultError) as info:
        await stack.vault.exchange(
            acme.id, link, "jira", await jira_code("acc-dave"), REDIRECT
        )  # Globex's site
    assert "different Jira site" in info.value.message


async def test_revoking_a_link_revokes_at_the_provider_and_deletes_the_secret(stack: Stack, env: Env) -> None:
    acme = env.world.acme
    link = connection(env, acme, "github", acme.user)
    code = (await consent("/github/login/oauth/authorize", "bob-gh", redirect_uri=REDIRECT, state="u"))[
        "code"
    ]
    assert (await stack.vault.exchange(acme.id, link, "github", code, REDIRECT))[
        "external_account_id"
    ] == "bob-gh"
    token = (await stack.vault.credential(acme.id, link, membership_id=acme.user)).token
    await stack.vault.revoke(acme.id, link)
    assert (
        await _get(f"{FAKES}/github/api/user", headers={"authorization": f"Bearer {token}"})
    ).status_code == 401
    assert sql(env, "select count(*) from app.connection_secrets where connection_id = %s", (link,)) == [(0,)]
    assert sql(env, "select status from app.connections where id = %s", (link,)) == [("revoked",)]
    with pytest.raises(VaultError) as info:
        await stack.vault.credential(acme.id, link, membership_id=acme.user)
    assert info.value.code == "not_active"


async def test_another_orgs_connection_is_not_found(stack: Stack, env: Env) -> None:
    acme, globex = env.world.acme, env.world.globex
    conn = connection(env, acme, "jira")
    await stack.vault.exchange(acme.id, conn, "jira", await jira_code("acc-alice"), REDIRECT)
    with pytest.raises(VaultError) as info:
        await stack.vault.credential(globex.id, conn)
    assert info.value.code == "not_found"


# ------------------------------------------------------------------ the HTTP service and its client


async def test_vault_service_requires_its_token_and_maps_errors(stack: Stack, env: Env) -> None:
    acme = env.world.acme
    app = create_vault_app(Settings(vault_service_token="service-token-for-tests"), stack.vault)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://vault") as raw:
        assert (await raw.get("/healthz")).status_code == 200
        assert (await raw.post("/v1/secrets/read", json={})).status_code == 401
        assert (
            await raw.post("/v1/secrets/read", json={}, headers={"authorization": "Bearer nope"})
        ).status_code == 401
    remote = RemoteVault(
        "http://vault", "service-token-for-tests", http=httpx.AsyncClient(transport=transport)
    )
    assert await remote.ready() == "ready"
    conn = connection(env, acme, "jira")
    await remote.store(acme.id, conn, "static-api-key")
    assert await remote.read(acme.id, conn) == "static-api-key"
    assert (await remote.credential(acme.id, conn)).kind == "static"
    with pytest.raises(VaultError) as info:
        await remote.credential(env.world.globex.id, conn)
    assert info.value.code == "not_found"
    await remote.delete(acme.id, conn)
    with pytest.raises(VaultError) as info:
        await remote.read(acme.id, conn)
    assert info.value.code == "not_found"
    wrong = RemoteVault("http://vault", "wrong", http=httpx.AsyncClient(transport=transport))
    with pytest.raises(VaultError):
        await wrong.read(acme.id, conn)
    with pytest.raises(VaultError):
        await wrong.ready()
    await remote.aclose()
    await wrong.aclose()

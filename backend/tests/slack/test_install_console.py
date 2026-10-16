"""Slack install (OAuth v2), console Slack routes, access request decisions."""

import json
import time
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import psycopg
import pytest
import pytest_asyncio

from app.auth.clerk import ClerkVerifier
from app.identity.clerk_api import ClerkClient
from app.slack import service, signature
from tests.console.conftest import ISSUER, ORIGIN, ClerkBackend, Jwks, SigningKey, mint
from tests.slack.conftest import FAKES, SIGNING_SECRET, Stack, app_for, fakes_up, seed_slack_token
from tests.world import World

pytestmark = pytest.mark.skipif(not fakes_up(), reason="Slack stand-in not reachable (run `make infra`)")

KEY = SigningKey()


@pytest.fixture(scope="module")
def clerk_backend() -> ClerkBackend:
    return ClerkBackend()


@pytest_asyncio.fixture(loop_scope="session")
async def console(stack: Stack, clerk_backend: ClerkBackend) -> AsyncIterator[httpx.AsyncClient]:
    jwks = Jwks([KEY])
    app = app_for(
        stack,
        clerk_verifier=ClerkVerifier(
            ISSUER,
            "https://clerk.test/jwks",
            [ORIGIN],
            http=httpx.AsyncClient(transport=httpx.MockTransport(jwks.handler)),
        ),
        clerk_client=ClerkClient(
            "https://api.clerk.test/v1",
            "sk_test_console",
            http=httpx.AsyncClient(transport=httpx.MockTransport(clerk_backend.handler)),
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://api"
    ) as client:
        yield client


def auth(sub: str, org: str) -> dict[str, str]:
    return {"authorization": f"Bearer {mint(KEY, sub, org)}", "origin": ORIGIN}


class Auth(Mapping[str, str]):
    """Headers with a token minted when they are used: tokens are short lived, and a module level
    token would expire while earlier test modules run."""

    def __init__(self, sub: str, org: str) -> None:
        self.sub, self.org = sub, org

    def __getitem__(self, key: str) -> str:
        return auth(self.sub, self.org)[key]

    def __iter__(self) -> Iterator[str]:
        return iter(("authorization", "origin"))

    def __len__(self) -> int:
        return 2


ALICE, BOB = Auth("user_alice", "org_acme"), Auth("user_bob", "org_acme")


def q(world: World, query: str, params: tuple[Any, ...] = ()) -> Any:
    with psycopg.connect(world.url) as conn:
        row = conn.execute(query, params).fetchone()
        return row[0] if row and len(row) == 1 else row


def sql(world: World, statement: str, params: tuple[Any, ...] = ()) -> None:
    with psycopg.connect(world.url) as conn:
        conn.execute(statement, params)


def fresh_org(world: World) -> tuple[str, str]:
    """An org with an admin and no Slack workspace: (clerk org id, clerk user id)."""
    tag = uuid.uuid4().hex[:6]
    sql(
        world,
        "select app.identity_upsert_membership(%s, %s, %s, %s, 'Founder', 'org:admin')",
        (f"org_s{tag}", f"Slackless {tag}", f"user_s{tag}", f"s-{tag}@x.test"),
    )
    return f"org_s{tag}", f"user_s{tag}"


async def new_team() -> tuple[str, str]:
    """A fresh, not yet installed workspace in the stand-in: (team id, team name)."""
    name = f"Team {uuid.uuid4().hex[:6]}"
    async with httpx.AsyncClient() as client:
        team = (await client.post(f"{FAKES}/_admin/slack/teams", json={"name": name})).json()["id"]
    return team, name


async def authorize_at_slack(install_url: str, team: str, decision: str = "allow") -> dict[str, list[str]]:
    params = parse_qs(urlparse(install_url).query)
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{FAKES}/slack/oauth/v2/authorize",
            data={
                "redirect_uri": params["redirect_uri"][0],
                "state": params["state"][0],
                "scope": params["scope"][0],
                "team": team,
                "decision": decision,
            },
        )
    return parse_qs(urlparse(response.headers["location"]).query)


async def callback(console: httpx.AsyncClient, query: dict[str, Any]) -> dict[str, list[str]]:
    flat = {k: v[0] if isinstance(v, list) else v for k, v in query.items()}
    response = await console.get(f"/slack/oauth/callback?{urlencode(flat)}")
    assert response.status_code == 302
    assert response.headers["location"].startswith("http://console.test/slack?")
    return parse_qs(urlparse(response.headers["location"]).query)


# ------------------------------------------------------------------ install


async def test_install_url(console: httpx.AsyncClient) -> None:
    body = (await console.get("/api/slack/install", headers=ALICE)).json()
    params = parse_qs(urlparse(body["url"]).query)
    assert body["url"].startswith(f"{FAKES}/slack/oauth/v2/authorize?")
    assert params["client_id"] == ["local.client"]
    assert params["redirect_uri"] == ["http://api.test/slack/oauth/callback"]
    assert set(params["scope"][0].split(",")) == set(service.BOT_SCOPES)
    assert (await console.get("/api/slack/install", headers=BOB)).status_code == 403


async def test_full_install_stores_the_token_encrypted(
    console: httpx.AsyncClient, stack: Stack, env: Any
) -> None:
    world = env.world
    org, user = fresh_org(world)
    stack.temporal.started.clear()
    team, name = await new_team()
    url = (await console.get("/api/slack/install", headers=auth(user, org))).json()["url"]
    granted = await authorize_at_slack(url, team)
    result = await callback(console, granted)
    assert result == {"installed": [name]}
    org_id = q(world, "select id from app.orgs where clerk_org_id = %s", (org,))
    assert q(world, "select name, bot_user_id, status from app.workspaces where org_id = %s", (org_id,)) == (
        name,
        "U" + team[1:] + "BOT",
        "active",
    )
    connection = q(
        world,
        "select id from app.connections where org_id = %s and integration_key = 'slack' and status = 'active'",
        (org_id,),
    )
    ciphertext = q(
        world, "select ciphertext from app.connection_secrets where connection_id = %s", (connection,)
    )
    assert b"xoxb" not in bytes(ciphertext)  # encrypted at rest
    bot_team, token = await service.bot_token(stack.pool, stack.vault, org_id)
    assert (bot_team, token.startswith(f"xoxb-{team}-")) == (team, True)
    assert [w for w, _, _ in stack.temporal.started] == ["SyncWorkspaceWorkflow"]
    assert (
        q(
            world,
            "select count(*) from app.audit_log where org_id = %s and action = 'slack.installed'",
            (org_id,),
        )
        == 1
    )
    # The same state cannot be replayed.
    assert (await callback(console, granted)) == {"error": ["state_reused"]}


async def test_install_errors(console: httpx.AsyncClient, env: Any) -> None:
    world = env.world
    org, user = fresh_org(world)
    url = (await console.get("/api/slack/install", headers=auth(user, org))).json()["url"]
    state = parse_qs(urlparse(url).query)["state"][0]
    assert await callback(console, {"code": "x", "state": state[:-2] + "00"}) == {"error": ["bad_state"]}
    assert await callback(console, {"code": "code-unknown", "state": state}) == {"error": ["exchange_failed"]}
    assert await callback(console, {"error": "access_denied", "state": state}) == {"error": ["access_denied"]}
    secret = "slack-state-secret-for-tests"
    org_id = q(world, "select id from app.orgs where clerk_org_id = %s", (org,))
    member = q(world, "select m.id from app.org_memberships m where m.org_id = %s", (org_id,))
    expired = service.make_state(secret, org_id, member, now=time.time() - 3600)
    assert await callback(console, {"code": "x", "state": expired}) == {"error": ["expired_state"]}
    # A workspace already connected to another org (Acme's) cannot be taken over.
    url2 = (await console.get("/api/slack/install", headers=auth(user, org))).json()["url"]
    assert await callback(console, await authorize_at_slack(url2, "T0ACME")) == {"error": ["workspace_taken"]}


async def test_token_storage_failure_undoes_the_install(
    console: httpx.AsyncClient, stack: Stack, env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install transaction commits before the token is stored (the vault's own connection must
    see the connection row). If storing fails, the install is undone and the token revoked at Slack;
    connecting again works."""
    world = env.world
    org, user = fresh_org(world)
    team, name = await new_team()
    tokens: list[str] = []

    async def broken_store(org_id: Any, connection_id: Any, secret: str) -> None:
        tokens.append(secret)
        raise RuntimeError("vault down")

    monkeypatch.setattr(stack.vault, "store", broken_store)
    url = (await console.get("/api/slack/install", headers=auth(user, org))).json()["url"]
    assert await callback(console, await authorize_at_slack(url, team)) == {"error": ["storage_failed"]}
    org_id = q(world, "select id from app.orgs where clerk_org_id = %s", (org,))
    assert q(world, "select status from app.workspaces where org_id = %s", (org_id,)) == "uninstalled"
    assert (
        q(
            world,
            "select count(*) from app.connections where org_id = %s and status = 'active'",
            (org_id,),
        )
        == 0
    )
    assert (await console.get("/api/slack", headers=auth(user, org))).json()["workspace"] is None
    async with httpx.AsyncClient() as client:
        check = (
            await client.post(
                f"{FAKES}/slack/api/auth.test", headers={"authorization": f"Bearer {tokens[0]}"}
            )
        ).json()
    assert check["ok"] is False  # revoked at Slack
    monkeypatch.undo()
    url = (await console.get("/api/slack/install", headers=auth(user, org))).json()["url"]
    assert await callback(console, await authorize_at_slack(url, team)) == {"installed": [name]}


async def test_an_org_keeps_one_workspace(console: httpx.AsyncClient) -> None:
    team, _ = await new_team()
    url = (await console.get("/api/slack/install", headers=ALICE)).json()["url"]
    assert await callback(console, await authorize_at_slack(url, team)) == {"error": ["other_workspace"]}


# ------------------------------------------------------------------ console Slack page


async def test_status_lists_channels(console: httpx.AsyncClient) -> None:
    body = (await console.get("/api/slack", headers=BOB)).json()
    assert body["workspace"]["external_id"] == "T0ACME"
    channels = {c["name"]: c for c in body["channels"]}
    assert channels["bugs"]["is_allowlisted"] is True
    assert channels["random"]["bot_is_member"] is False
    assert body["install_configured"] is True


async def test_allowlist_toggle(console: httpx.AsyncClient, stack: Stack, env: Any) -> None:
    world = env.world
    stack.temporal.started.clear()
    random = q(world, "select id from app.channels where external_id = 'C0RANDOM'")
    response = await console.patch(
        f"/api/slack/channels/{random}", json={"is_allowlisted": True}, headers=ALICE
    )
    assert response.json()["is_allowlisted"] is True
    assert stack.temporal.started == []  # the bot is not in #random: nothing to sync yet
    bugs = q(world, "select id from app.channels where external_id = 'C0BUGS'")
    sql(world, "update app.channels set archived = false, is_allowlisted = true where id = %s", (bugs,))
    sql(
        world,
        "update app.schedules set status = 'active', pause_reason = null where id = %s",
        (world.acme.ids["schedules"],),
    )
    removed = await console.patch(
        f"/api/slack/channels/{bugs}", json={"is_allowlisted": False}, headers=ALICE
    )
    assert removed.json()["paused_schedules"] == 1
    back = await console.patch(f"/api/slack/channels/{bugs}", json={"is_allowlisted": True}, headers=ALICE)
    assert back.json()["is_allowlisted"] is True
    assert [w for w, _, _ in stack.temporal.started] == [
        "ScheduleSyncWorkflow",  # the paused schedule stops firing and admins hear about it
        "SyncWorkspaceWorkflow",  # members of a newly enabled channel
    ]
    old = q(world, "select id from app.channels where external_id = 'C0OLDPROJ'")
    assert (
        await console.patch(f"/api/slack/channels/{old}", json={"is_allowlisted": True}, headers=ALICE)
    ).status_code == 409
    other = q(world, "select id from app.channels where external_id = 'C0GXGENERAL'")
    assert (
        await console.patch(f"/api/slack/channels/{other}", json={"is_allowlisted": False}, headers=ALICE)
    ).status_code == 404
    assert (
        await console.patch(f"/api/slack/channels/{random}", json={"is_allowlisted": False}, headers=BOB)
    ).status_code == 403


async def test_sync_now(console: httpx.AsyncClient, stack: Stack) -> None:
    stack.temporal.started.clear()
    assert (await console.post("/api/slack/sync", headers=ALICE)).status_code == 202
    assert [w for w, _, _ in stack.temporal.started] == ["SyncWorkspaceWorkflow"]


async def test_disconnect(console: httpx.AsyncClient, stack: Stack, env: Any) -> None:
    world = env.world
    org, user = fresh_org(world)
    team, _ = await new_team()
    url = (await console.get("/api/slack/install", headers=auth(user, org))).json()["url"]
    await callback(console, await authorize_at_slack(url, team))
    org_id = q(world, "select id from app.orgs where clerk_org_id = %s", (org,))
    _, token = await service.bot_token(stack.pool, stack.vault, org_id)
    assert (await console.delete("/api/slack", headers=auth(user, org))).status_code == 204
    assert (await console.get("/api/slack", headers=auth(user, org))).json()["workspace"] is None
    assert (
        q(
            world,
            "select count(*) from app.connection_secrets s join app.connections c on c.id = s.connection_id where c.org_id = %s",
            (org_id,),
        )
        == 0
    )
    async with httpx.AsyncClient() as client:
        revoked = (
            await client.post(f"{FAKES}/slack/api/auth.test", headers={"authorization": f"Bearer {token}"})
        ).json()
    assert revoked["error"] in ("invalid_auth", "account_inactive")
    assert (await console.delete("/api/slack", headers=auth(user, org))).status_code == 409


# ------------------------------------------------------------------ access requests


def pending_request(world: World, user: str, email: str | None) -> str:
    workspace = q(world, "select id from app.workspaces where external_id = 'T0ACME'")
    sql(
        world,
        "update app.access_requests set status = 'dismissed', resolved_at = now() where external_user_id = %s and status = 'pending'",
        (user,),
    )
    return str(
        q(
            world,
            "insert into app.access_requests (org_id, workspace_id, external_user_id, email) values (%s, %s, %s, %s) returning id",
            (world.acme.id, workspace, user, email),
        )
    )


async def test_console_invite_and_dismiss(
    console: httpx.AsyncClient, clerk_backend: ClerkBackend, env: Any
) -> None:
    world = env.world
    email = f"req-{uuid.uuid4().hex[:6]}@acme.test"
    request_id = pending_request(world, f"U0R{uuid.uuid4().hex[:5].upper()}", email)
    invited = await console.post(f"/api/access-requests/{request_id}/invite", headers=ALICE)
    assert invited.json()["status"] == "approved"
    assert any(body and body.get("email_address") == email for _, _, body in clerk_backend.calls)
    assert (
        q(
            world,
            "select m.status from app.org_memberships m join app.users u on u.id = m.user_id where u.email = %s",
            (email,),
        )
        == "invited"
    )
    assert (await console.post(f"/api/access-requests/{request_id}/invite", headers=ALICE)).status_code == 409
    other = pending_request(world, f"U0R{uuid.uuid4().hex[:5].upper()}", None)
    assert (await console.post(f"/api/access-requests/{other}/invite", headers=ALICE)).json()["error"][
        "code"
    ] == "no_email"
    assert (await console.post(f"/api/access-requests/{other}/dismiss", headers=ALICE)).json()[
        "status"
    ] == "dismissed"
    assert (await console.post(f"/api/access-requests/{other}/dismiss", headers=BOB)).status_code == 403


async def interaction(console: httpx.AsyncClient, user: str, action_id: str, value: str) -> httpx.Response:
    payload = {
        "type": "block_actions",
        "user": {"id": user},
        "team": {"id": "T0ACME"},
        "container": {"type": "message", "channel_id": f"D{user[1:]}", "message_ts": "1.1"},
        "actions": [{"action_id": action_id, "value": value, "action_ts": "1.1"}],
    }
    body = urlencode({"payload": json.dumps(payload)}).encode()
    ts = int(time.time())
    headers = {
        "x-slack-request-timestamp": str(ts),
        "x-slack-signature": signature.sign(SIGNING_SECRET, ts, body),
        "content-type": "application/x-www-form-urlencoded",
    }
    return await console.post("/slack/interactions", content=body, headers=headers)


async def test_slack_invite_button(
    console: httpx.AsyncClient, stack: Stack, clerk_backend: ClerkBackend, env: Any
) -> None:
    world = env.world
    await seed_slack_token(stack, world.acme.id, "T0ACME")
    email = f"btn-{uuid.uuid4().hex[:6]}@acme.test"
    request_id = pending_request(world, f"U0B{uuid.uuid4().hex[:5].upper()}", email)
    # A member who is not an admin cannot decide.
    assert (await interaction(console, "U0BOB", "access_request_invite", request_id)).status_code == 200
    assert q(world, "select status from app.access_requests where id = %s", (request_id,)) == "pending"
    assert (await interaction(console, "U0ALICE", "access_request_invite", request_id)).status_code == 200
    assert q(world, "select status from app.access_requests where id = %s", (request_id,)) == "approved"
    assert (
        q(
            world,
            "select m.status from app.org_memberships m join app.users u on u.id = m.user_id where u.email = %s",
            (email,),
        )
        == "invited"
    )
    dismissed = pending_request(world, f"U0D{uuid.uuid4().hex[:5].upper()}", "d@acme.test")
    await interaction(console, "U0ALICE", "access_request_dismiss", dismissed)
    assert q(world, "select status from app.access_requests where id = %s", (dismissed,)) == "dismissed"

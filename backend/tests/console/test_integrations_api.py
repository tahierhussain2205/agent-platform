"""Console integrations: connect (admins) and link (everyone) through the stand-ins' consent pages,
OAuth state attacks, overrides, disconnect, custom MCP approval, health, and tenant isolation."""

from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
import pytest_asyncio

from app.auth.clerk import ClerkVerifier
from tests.connect import sql
from tests.console.conftest import ISSUER, ORIGIN, Jwks, SigningKey, mint
from tests.stack import FAKES, Stack, app_for, fakes_up

pytestmark = pytest.mark.skipif(not fakes_up(), reason="stand-ins not reachable (run `make infra`)")
KEY = SigningKey()


class Auth(Mapping[str, str]):
    """Console headers with a fresh token on every use."""

    def __init__(self, sub: str, org: str) -> None:
        self.sub, self.org = sub, org

    def __getitem__(self, key: str) -> str:
        return {"authorization": f"Bearer {mint(KEY, self.sub, self.org)}", "origin": ORIGIN}[key]

    def __iter__(self) -> Iterator[str]:
        return iter(("authorization", "origin"))

    def __len__(self) -> int:
        return 2


ALICE, BOB, DAVE = (
    Auth("user_alice", "org_acme"),
    Auth("user_bob", "org_acme"),
    Auth("user_dave", "org_globex"),
)


@pytest.fixture(autouse=True)
def _integration_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "OAUTH_STATE_SECRET": "oauth-state-secret-for-tests",
        "API_PUBLIC_URL": "http://localhost:8000",  # matches the GitHub App setup URL in the stand-in
        "CONSOLE_URL": "http://console.test",
        "ATLASSIAN_AUTHORIZE_URL": f"{FAKES}/atlassian/authorize",
        "ATLASSIAN_API_URL": f"{FAKES}/atlassian/api",
        "ATLASSIAN_CLIENT_ID": "local-jira-client",
        "GITHUB_WEB_URL": f"{FAKES}/github",
        "GITHUB_API_URL": f"{FAKES}/github/api",
        "GITHUB_APP_SLUG": "agent-platform-local",
        "GITHUB_CLIENT_ID": "Iv1.localclient",
        "GOOGLE_AUTH_URL": f"{FAKES}/google/o/oauth2/v2/auth",
        "GOOGLE_CLIENT_ID": "local-google-client",
        "GOOGLE_MCP_URL": f"{FAKES}/google/mcp",
        "GCP_LOGGING_URL": f"{FAKES}/gcp/logging",
    }.items():
        monkeypatch.setenv(name, value)


@pytest_asyncio.fixture(loop_scope="session")
async def console(stack: Stack) -> AsyncIterator[httpx.AsyncClient]:
    verifier = ClerkVerifier(
        ISSUER,
        "https://clerk.test/jwks",
        [ORIGIN],
        http=httpx.AsyncClient(transport=httpx.MockTransport(Jwks([KEY]).handler)),
    )
    app = app_for(stack, clerk_verifier=verifier)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://api"
    ) as client:
        yield client


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


async def consent(url: str, account: str) -> dict[str, str]:
    """Allow on the stand-in's consent page for the authorize URL the API built."""
    params = query(url)
    path = urlparse(url).path
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{FAKES}{path}", data={**params, "account": account, "decision": "allow"}
        )
    assert response.status_code == 302, response.text
    return query(response.headers["location"])


async def callback(console: httpx.AsyncClient, path: str, params: dict[str, str]) -> dict[str, str]:
    response = await console.get(f"{path}?{urlencode(params)}")
    assert response.status_code == 302, response.text
    assert response.headers["location"].startswith("http://console.test/integrations?")
    return query(response.headers["location"])


async def connect(
    console: httpx.AsyncClient, key: str, account: str, who: Any = ALICE, action: str = "connect"
) -> dict[str, str]:
    response = await console.get(f"/api/integrations/{key}/{action}", headers=who)
    assert response.status_code == 200, response.text
    granted = await consent(response.json()["url"], account)
    if key == "github" and action == "connect":
        return await callback(console, "/oauth/github/install", granted)
    return await callback(console, f"/oauth/{key}/callback", granted)


async def integrations(console: httpx.AsyncClient, who: Any) -> dict[str, Any]:
    response = await console.get("/api/integrations", headers=who)
    assert response.status_code == 200, response.text
    return {i["key"]: i for i in response.json()["integrations"]}


async def test_catalog_listing(console: httpx.AsyncClient) -> None:
    listed = await integrations(console, BOB)
    assert list(listed)[:5] == ["slack", "jira", "github", "google_cloud_logging", "google_workspace"]
    logs = listed["google_cloud_logging"]
    assert (logs["auth"], logs["user_links"], logs["configured"]) == ("service_account", False, True)
    assert {o["name"]: (o["tier"], o["default_mode"]) for o in logs["operations"]} == {
        "search_events": ("read", "org"), "list_error_groups": ("read", "org"), "get_trace": ("read", "org"),
    }  # fmt: skip
    jira = listed["jira"]
    assert (jira["configured"], jira["user_links"], jira["kind"]) == (True, True, "native")
    ops = {o["name"]: o for o in jira["operations"]}
    assert ops["get_issue"]["default_mode"] == "org_with_check"
    assert ops["get_issue"]["tier"] == "read"


async def test_admins_connect_jira_and_state_cannot_be_replayed(
    console: httpx.AsyncClient, stack: Stack
) -> None:
    assert (await console.get("/api/integrations/jira/connect", headers=BOB)).status_code == 403
    url = (await console.get("/api/integrations/jira/connect", headers=ALICE)).json()["url"]
    assert query(url)["redirect_uri"] == "http://localhost:8000/oauth/jira/callback"
    granted = await consent(url, "acc-alice")
    assert await callback(console, "/oauth/jira/callback", granted) == {"connected": "jira"}
    assert await callback(console, "/oauth/jira/callback", granted) == {"error": "state_reused"}
    forged = {**granted, "state": granted["state"][:-4] + "beef"}
    assert await callback(console, "/oauth/jira/callback", forged) == {"error": "bad_state"}
    listed = await integrations(console, BOB)
    assert listed["jira"]["enabled"] is True  # connecting enables it
    assert listed["jira"]["connection"]["external_account_id"] == "cloud-acme"
    assert listed["jira"]["connection"]["external_account_label"] == "https://acme.atlassian.net"
    # Reconnecting replaces the connection.
    await connect(console, "jira", "acc-alice")
    acme = stack.env.world.acme
    active = sql(stack, "select count(*) from app.connections where org_id = %s and integration_key = 'jira' "
                        "and scope = 'org' and status = 'active'", (acme.id,))  # fmt: skip
    assert active == [(1,)]
    audited = sql(stack, "select count(*) from app.audit_log where org_id = %s and action = 'integration.connected'",
                  (acme.id,))  # fmt: skip
    assert audited[0][0] >= 2


async def test_people_link_their_own_accounts(console: httpx.AsyncClient, stack: Stack) -> None:
    await connect(console, "jira", "acc-alice")
    assert await connect(console, "jira", "acc-bob", BOB, "link") == {"linked": "jira"}
    bob_view = await integrations(console, BOB)
    assert bob_view["jira"]["link"]["external_account_id"] == "acc-bob"
    assert bob_view["jira"]["link"]["external_account_label"] == "Bob Builder"
    alice_view = await integrations(console, ALICE)
    assert alice_view["jira"]["link"] is None  # links are personal
    # Google links use PKCE: the verifier stays on the server.
    url = (await console.get("/api/integrations/google_workspace/link", headers=BOB)).status_code
    assert url == 409  # not enabled yet
    await console.patch("/api/integrations/google_workspace", json={"enabled": True}, headers=ALICE)
    link_url = (await console.get("/api/integrations/google_workspace/link", headers=BOB)).json()["url"]
    assert query(link_url)["code_challenge_method"] == "S256"
    granted = await consent(link_url, "bob@acme.test")
    assert await callback(console, "/oauth/google_workspace/callback", granted) == {
        "linked": "google_workspace"
    }
    assert (await console.delete("/api/integrations/google_workspace/link", headers=BOB)).status_code == 204
    assert (await integrations(console, BOB))["google_workspace"]["link"] is None


async def test_github_app_installation(console: httpx.AsyncClient) -> None:
    url = (await console.get("/api/integrations/github/connect", headers=ALICE)).json()["url"]
    assert urlparse(url).path == "/github/apps/agent-platform-local/installations/new"
    granted = await consent(url, "alice-gh|acme")
    assert await callback(console, "/oauth/github/install", granted) == {"connected": "github"}
    github = (await integrations(console, ALICE))["github"]
    assert github["connection"]["external_account_id"] == "acme"
    assert github["connection"]["external_account_label"] == "acme"
    checked = await console.post("/api/integrations/github/check", headers=ALICE)
    assert checked.json() == {"key": "github", "health": "ok", "detail": "3 repositories visible."}
    # An installation that is not the installer's is refused (and the row revoked).
    url = (await console.get("/api/integrations/github/connect", headers=ALICE)).json()["url"]
    granted = await consent(url, "alice-gh|acme")
    other = await consent(
        f"{FAKES}/github/apps/agent-platform-local/installations/new?state=x", "dave-gh|globex"
    )
    forged = {**granted, "installation_id": other["installation_id"]}
    assert (await callback(console, "/oauth/github/install", forged))["error"] == "not_yours"


async def test_overrides_and_disconnect(console: httpx.AsyncClient, stack: Stack) -> None:
    await connect(console, "jira", "acc-alice")
    assert (
        await console.put(
            "/api/integrations/jira/operations/get_issue",
            json={"credential_mode": "user_required"},
            headers=BOB,
        )
    ).status_code == 403
    assert (
        await console.put(
            "/api/integrations/jira/operations/get_issue", json={"credential_mode": "nope"}, headers=ALICE
        )
    ).status_code == 422
    assert (
        await console.put(
            "/api/integrations/jira/operations/nope", json={"credential_mode": "org"}, headers=ALICE
        )
    ).status_code == 404
    no_links = await console.put(  # nobody can link a Slack account: it would only break the agent
        "/api/integrations/slack/operations/read_channel",
        json={"credential_mode": "user_required"},
        headers=ALICE,
    )
    assert (no_links.status_code, no_links.json()["error"]["code"]) == (409, "no_links")
    set_ = await console.put(
        "/api/integrations/jira/operations/get_issue",
        json={"credential_mode": "user_required", "enabled": True},
        headers=ALICE,
    )
    assert set_.status_code == 200
    op = next(o for o in (await integrations(console, BOB))["jira"]["operations"] if o["name"] == "get_issue")
    assert (op["mode"], op["overridden"]) == ("user_required", True)
    assert (
        await console.delete("/api/integrations/jira/operations/get_issue", headers=ALICE)
    ).status_code == 204
    op = next(o for o in (await integrations(console, BOB))["jira"]["operations"] if o["name"] == "get_issue")
    assert (op["mode"], op["overridden"]) == ("org_with_check", False)
    assert (await console.delete("/api/integrations/jira/connection", headers=BOB)).status_code == 403
    assert (await console.delete("/api/integrations/jira/connection", headers=ALICE)).status_code == 204
    assert (await integrations(console, ALICE))["jira"]["connection"] is None
    assert (await console.delete("/api/integrations/jira/connection", headers=ALICE)).status_code == 404


async def test_custom_mcp_servers_need_approval(console: httpx.AsyncClient) -> None:
    body = {"title": "Status Page", "url": f"{FAKES}/mcp/status", "api_key": "mcp-key-local"}
    assert (await console.post("/api/integrations/custom-mcp", json=body, headers=BOB)).status_code == 403
    added = await console.post("/api/integrations/custom-mcp", json=body, headers=ALICE)
    assert added.json() == {"key": "mcp_status_page", "enabled": False, "approved": False}
    assert (await console.post("/api/integrations/custom-mcp", json=body, headers=ALICE)).status_code == 409
    listed = (await integrations(console, ALICE))["mcp_status_page"]
    assert (listed["approved"], listed["enabled"], listed["operations"]) == (False, False, [])
    refused = await console.patch("/api/integrations/mcp_status_page", json={"enabled": True}, headers=ALICE)
    assert refused.json()["error"]["code"] == "approval_required"
    approved = await console.post("/api/integrations/mcp_status_page/approve", headers=ALICE)
    assert approved.json() == {"key": "mcp_status_page", "health": "ok", "detail": "2 tools available."}
    assert (
        await console.patch("/api/integrations/mcp_status_page", json={"enabled": True}, headers=ALICE)
    ).status_code == 200
    listed = (await integrations(console, BOB))["mcp_status_page"]
    tiers = {o["name"]: o["tier"] for o in listed["operations"]}
    assert tiers == {"status_incidents": "read", "restart_service": "irreversible"}


async def test_another_org_sees_and_touches_nothing(console: httpx.AsyncClient) -> None:
    await connect(console, "jira", "acc-alice")
    acme_conn = (await integrations(console, ALICE))["jira"]["connection"]
    globex = await integrations(console, DAVE)
    own = globex["jira"]["connection"]  # the test world gives every org a Jira connection of its own
    assert own is None or (own["id"] != acme_conn["id"] and own["external_account_id"] != "cloud-acme")
    await console.delete("/api/integrations/jira/connection", headers=DAVE)  # Globex's own, if any
    assert (await integrations(console, ALICE))["jira"]["connection"]["id"] == acme_conn["id"]
    assert "mcp_status_page" not in globex


async def test_admins_connect_cloud_logging_with_a_service_account_key(
    console: httpx.AsyncClient, stack: Stack
) -> None:
    import json

    from app.settings import get_settings
    from tests.stack import GCP_KEY

    get_settings.cache_clear()
    path = "/api/integrations/google_cloud_logging/service-account"
    assert (await console.post(path, json={"key_json": GCP_KEY}, headers=BOB)).status_code == 403
    wrong = await console.post(
        "/api/integrations/jira/service-account", json={"key_json": GCP_KEY}, headers=ALICE
    )
    assert wrong.status_code == 404
    first = await console.post(path, json={"key_json": GCP_KEY}, headers=ALICE)
    assert first.status_code == 200, first.text
    assert first.json() == {"key": "google_cloud_logging", "account": "agent-reader@acme-prod.iam.gserviceaccount.com",
                            "project_id": "acme-prod"}  # fmt: skip
    active = (
        "select id from app.connections where integration_key = 'google_cloud_logging' and status = 'active'"
    )
    (connected,) = sql(stack, active)
    # A key Google refuses changes nothing: the working connection stays.
    refused_key = json.dumps(
        {**json.loads(GCP_KEY), "client_email": "x@other-project.iam.gserviceaccount.com"}
    )
    refused = await console.post(path, json={"key_json": refused_key}, headers=ALICE)
    assert (refused.status_code, refused.json()["error"]["code"]) == (502, "provider_refused")
    assert sql(stack, active) == [connected]
    bad = await console.post(path, json={"key_json": '{"type": "authorized_user", "x": 1}'}, headers=ALICE)
    assert (bad.status_code, bad.json()["error"]["code"]) == (422, "invalid_key")
    assert sql(stack, active) == [connected]
    assert "PRIVATE KEY" not in bad.text
    # A new key replaces the old one.
    again = await console.post(path, json={"key_json": GCP_KEY}, headers=ALICE)
    assert again.status_code == 200
    assert sql(stack, active) != [connected]
    health = await console.post("/api/integrations/google_cloud_logging/check", headers=ALICE)
    assert health.json() == {
        "key": "google_cloud_logging",
        "health": "ok",
        "detail": "Logs of project acme-prod are readable.",
    }
    actions = [
        r[0]
        for r in sql(
            stack,
            "select action from app.audit_log where target = 'google_cloud_logging' order by occurred_at",
        )
    ]
    assert actions.count("integration.connected") == 2
    assert actions.count("integration.connect_failed") == 2

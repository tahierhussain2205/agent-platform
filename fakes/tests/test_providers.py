"""The Jira, GitHub and Google stand-ins behave like the parts of those APIs the platform uses."""

import base64
import hashlib
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from fakes.config import Config
from fakes.main import create_app

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC = (
    KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
)
JIRA = {"client_id": "local-jira-client", "client_secret": "local-jira-client-secret"}


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    config = Config(state_dir=tmp_path, seed_fixture=tmp_path / "none.json", github_app_public_key=PUBLIC)
    with TestClient(create_app(config, persist=False), follow_redirects=False) as c:
        yield c


def query(response: Any) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(response.headers["location"]).query).items()}


# ------------------------------------------------------------------ Jira


def jira_tokens(client: TestClient, account: str = "acc-bob") -> dict[str, Any]:
    page = client.get(
        "/atlassian/authorize",
        params={
            "client_id": JIRA["client_id"],
            "redirect_uri": "http://app/cb",
            "state": "s1",
            "scope": "read:jira-work offline_access",
        },
    )
    assert page.status_code == 200
    assert "Authorize as" in page.text
    granted = client.post(
        "/atlassian/authorize",
        data={
            "redirect_uri": "http://app/cb",
            "state": "s1",
            "scope": "read:jira-work offline_access",
            "account": account,
            "decision": "allow",
        },
    )
    code = query(granted)["code"]
    assert query(granted)["state"] == "s1"
    tokens = client.post(
        "/atlassian/oauth/token",
        json={**JIRA, "grant_type": "authorization_code", "code": code, "redirect_uri": "http://app/cb"},
    )
    assert tokens.status_code == 200
    again = client.post(
        "/atlassian/oauth/token",
        json={**JIRA, "grant_type": "authorization_code", "code": code, "redirect_uri": "http://app/cb"},
    )
    assert again.json()["error"] == "invalid_grant"  # codes are single use
    data: dict[str, Any] = tokens.json()
    return data


def jira(client: TestClient, token: str, path: str, method: str = "GET", **kw: Any) -> Any:
    return client.request(
        method,
        f"/atlassian/api/ex/jira/cloud-acme/rest/api/3/{path}",
        headers={"authorization": f"Bearer {token}"},
        **kw,
    )


def test_jira_oauth_refresh_rotates(client: TestClient) -> None:
    tokens = jira_tokens(client)
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 3600
    refreshed = client.post(
        "/atlassian/oauth/token", json={**JIRA, "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["refresh_token"] != tokens["refresh_token"]
    reused = client.post(
        "/atlassian/oauth/token", json={**JIRA, "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
    )
    assert reused.json()["error"] == "invalid_grant"  # rotating refresh tokens are single use
    bad = client.post(
        "/atlassian/oauth/token",
        json={**JIRA, "client_secret": "nope", "grant_type": "refresh_token", "refresh_token": "x"},
    )
    assert bad.status_code == 401
    denied = client.post(
        "/atlassian/authorize",
        data={"redirect_uri": "http://app/cb", "state": "s", "account": "acc-bob", "decision": "deny"},
    )
    assert query(denied)["error"] == "access_denied"


def test_jira_reads_follow_the_tokens_account(client: TestClient) -> None:
    bob = jira_tokens(client, "acc-bob")["access_token"]
    alice = jira_tokens(client, "acc-alice")["access_token"]
    sites = client.get(
        "/atlassian/api/oauth/token/accessible-resources", headers={"authorization": f"Bearer {bob}"}
    ).json()
    assert sites[0]["id"] == "cloud-acme"
    assert jira(client, bob, "myself").json()["accountId"] == "acc-bob"
    bob_keys = {
        i["key"]
        for i in jira(client, bob, "search/jql", "POST", json={"jql": "ORDER BY updated DESC"}).json()["issues"]
    }
    alice_keys = {i["key"] for i in jira(client, alice, "search/jql", "POST", json={"jql": ""}).json()["issues"]}
    assert "SEC-3" in alice_keys
    assert "SEC-3" not in bob_keys  # Bob cannot browse the Security project
    assert "OPS-1" not in alice_keys  # another site
    assert jira(client, bob, "issue/SEC-3").status_code == 404
    issue = jira(client, bob, "issue/PAY-101").json()
    assert issue["fields"]["summary"].startswith("Checkout returns 500")
    assert issue["fields"]["description"]["type"] == "doc"
    assert [p["key"] for p in jira(client, bob, "project/search").json()["values"]] == ["PAY", "WEB"]
    globex = client.get(
        "/atlassian/api/ex/jira/cloud-globex/rest/api/3/myself", headers={"authorization": f"Bearer {bob}"}
    )
    assert globex.status_code == 404


def test_jira_jql_and_permission_lookups(client: TestClient) -> None:
    alice = jira_tokens(client, "acc-alice")["access_token"]

    def keys(jql: str) -> list[str]:
        return sorted(i["key"] for i in jira(client, alice, "search/jql", "POST", json={"jql": jql}).json()["issues"])

    assert keys("project = PAY") == ["PAY-101", "PAY-102", "PAY-103", "PAY-104", "PAY-105", "PAY-106"]
    assert keys('project in (PAY, WEB) AND status = "In Progress"') == ["PAY-101", "WEB-7"]
    assert keys('text ~ "checkout"') == ["PAY-101"]
    assert keys("assignee = currentUser()") == ["PAY-103", "PAY-106", "SEC-3"]
    # What progress reports ask: created, resolved, open by status category.
    assert keys("project = PAY AND created >= -1d") == ["PAY-101", "PAY-104", "PAY-106"]
    assert keys("project = PAY AND resolved >= -1d") == ["PAY-105", "PAY-106"]
    assert keys("project = PAY AND resolutiondate < -1d") == ["PAY-103"]
    assert keys("project = PAY AND statusCategory != Done") == ["PAY-101", "PAY-102", "PAY-104"]
    assert keys('project = PAY AND statusCategory in ("To Do", indeterminate)') == ["PAY-101", "PAY-102", "PAY-104"]
    assert keys('project = PAY AND created >= "2000-01-01" AND created < "2000-01-02 10:30"') == []
    assert jira(client, alice, "search/jql", "POST", json={"jql": 'created >= "yesterday"'}).status_code == 400
    done = jira(client, alice, "issue/PAY-105").json()["fields"]
    assert done["status"]["statusCategory"]["key"] == "done"
    assert done["resolutiondate"] == done["updated"]
    assert jira(client, alice, "issue/PAY-101").json()["fields"]["resolutiondate"] is None
    assert keys("updated >= -2d AND project = WEB") == ["WEB-7", "WEB-8"]
    assert keys("updated >= -13h AND project = WEB") == ["WEB-8"]
    assert jira(client, alice, "search/jql", "POST", json={"jql": "nonsense ~~ x"}).status_code == 400
    page = jira(client, alice, "search/jql", "POST", json={"jql": "", "maxResults": 2}).json()
    assert (len(page["issues"]), page["isLast"], page["nextPageToken"]) == (2, False, "2")
    found = jira(client, alice, "user/search", params={"query": "bob@acme.test"}).json()
    assert [u["accountId"] for u in found] == ["acc-bob"]
    can = jira(
        client,
        alice,
        "user/permission/search",
        params={"permissions": "BROWSE_PROJECTS", "projectKey": "SEC", "accountId": "acc-bob"},
    )
    assert can.json() == []
    assert (
        jira(
            client,
            alice,
            "user/permission/search",
            params={"permissions": "BROWSE_PROJECTS", "projectKey": "PAY", "accountId": "acc-bob"},
        ).json()[0]["accountId"]
        == "acc-bob"
    )


def test_jira_expired_tokens_are_refused(client: TestClient) -> None:
    token = jira_tokens(client)["access_token"]
    client.post("/_admin/atlassian/expire-tokens")
    assert jira(client, token, "myself").status_code == 401


# ------------------------------------------------------------------ GitHub


def app_jwt(iss: str = "424242", key: Any = KEY, life: int = 540) -> str:
    now = int(time.time())
    return jwt.encode({"iat": now - 30, "exp": now + life, "iss": iss}, key, algorithm="RS256")


def install(client: TestClient, account: str = "alice-gh|acme") -> dict[str, str]:
    page = client.get("/github/apps/agent-platform-local/installations/new", params={"state": "st"})
    assert "alice-gh installing on acme" in page.text
    return query(
        client.post(
            "/github/apps/agent-platform-local/installations/new",
            data={"state": "st", "account": account, "decision": "allow"},
        )
    )


def gh_user_token(client: TestClient, code: str) -> dict[str, Any]:
    body: dict[str, Any] = client.post(
        "/github/login/oauth/access_token",
        json={"client_id": "Iv1.localclient", "client_secret": "local-github-client-secret", "code": code},
        headers={"accept": "application/json"},
    ).json()
    return body


def test_github_installation_flow_and_installation_tokens(client: TestClient) -> None:
    setup = install(client)
    assert setup["setup_action"] == "install"
    assert setup["state"] == "st"
    user = gh_user_token(client, setup["code"])
    assert user["expires_in"] == 28800
    mine = client.get(
        "/github/api/user/installations", headers={"authorization": f"Bearer {user['access_token']}"}
    ).json()
    assert [i["id"] for i in mine["installations"]] == [int(setup["installation_id"])]
    token = client.post(
        f"/github/api/app/installations/{setup['installation_id']}/access_tokens",
        headers={"authorization": f"Bearer {app_jwt()}"},
    )
    assert token.status_code == 201
    repos = client.get(
        "/github/api/installation/repositories", headers={"authorization": f"Bearer {token.json()['token']}"}
    ).json()
    assert [r["full_name"] for r in repos["repositories"]] == ["acme/cart-service", "acme/infra", "acme/web-app"]
    # A JWT from the wrong key, the wrong app, or too long lived is refused.
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for bad in (app_jwt(key=other), app_jwt(iss="1"), app_jwt(life=3600)):
        assert (
            client.post(
                f"/github/api/app/installations/{setup['installation_id']}/access_tokens",
                headers={"authorization": f"Bearer {bad}"},
            ).status_code
            == 401
        )


def test_github_permissions_contents_and_revocation(client: TestClient) -> None:
    setup = install(client)
    token = client.post(
        f"/github/api/app/installations/{setup['installation_id']}/access_tokens",
        headers={"authorization": f"Bearer {app_jwt()}"},
    ).json()["token"]
    inst = {"authorization": f"token {token}"}
    perm = client.get("/github/api/repos/acme/infra/collaborators/bob-gh/permission", headers=inst).json()
    assert perm["permission"] == "none"
    assert (
        client.get("/github/api/repos/acme/cart-service/collaborators/bob-gh/permission", headers=inst).json()[
            "permission"
        ]
        == "write"
    )
    assert (
        client.get("/github/api/repos/acme/web-app/collaborators/bob-gh/permission", headers=inst).json()["permission"]
        == "read"
    )  # public, org member
    content = client.get("/github/api/repos/acme/cart-service/contents/CODEOWNERS", headers=inst).json()
    assert base64.b64decode(content["content"]).decode().startswith("* @acme/platform")
    assert (
        client.get("/github/api/repos/globex/payments/contents/README.md", headers=inst).status_code == 404
    )  # another installation
    # User tokens see only what the user can read.
    page = client.get(
        "/github/login/oauth/authorize",
        params={"client_id": "Iv1.localclient", "redirect_uri": "http://app/cb", "state": "u"},
    )
    assert page.status_code == 200
    code = query(
        client.post(
            "/github/login/oauth/authorize",
            data={"redirect_uri": "http://app/cb", "state": "u", "account": "bob-gh", "decision": "allow"},
        )
    )["code"]
    bob = gh_user_token(client, code)
    as_bob = {"authorization": f"Bearer {bob['access_token']}"}
    assert client.get("/github/api/user", headers=as_bob).json()["login"] == "bob-gh"
    assert client.get("/github/api/repos/acme/infra/contents/README.md", headers=as_bob).status_code == 404
    assert client.get("/github/api/repos/acme/cart-service/contents/README.md", headers=as_bob).status_code == 200
    refreshed = client.post(
        "/github/login/oauth/access_token",
        data={
            "client_id": "Iv1.localclient",
            "client_secret": "local-github-client-secret",
            "grant_type": "refresh_token",
            "refresh_token": bob["refresh_token"],
        },
    ).json()
    assert refreshed["access_token"] != bob["access_token"]
    basic = base64.b64encode(b"Iv1.localclient:local-github-client-secret").decode()
    gone = client.request(
        "DELETE",
        "/github/api/applications/Iv1.localclient/token",
        json={"access_token": refreshed["access_token"]},
        headers={"authorization": f"Basic {basic}"},
    )
    assert gone.status_code == 204
    assert (
        client.get("/github/api/user", headers={"authorization": f"Bearer {refreshed['access_token']}"}).status_code
        == 401
    )


# ------------------------------------------------------------------ Google and MCP


def google_tokens(client: TestClient, email: str = "alice@acme.test") -> dict[str, Any]:
    verifier = "v" * 50
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {
        "client_id": "local-google-client",
        "redirect_uri": "http://app/cb",
        "state": "g",
        "scope": "drive.readonly",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    assert client.get("/google/o/oauth2/v2/auth", params=params).status_code == 200
    code = query(client.post("/google/o/oauth2/v2/auth", data={**params, "account": email, "decision": "allow"}))[
        "code"
    ]
    form = {
        "client_id": "local-google-client",
        "client_secret": "local-google-client-secret",
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://app/cb",
    }
    assert client.post("/google/token", data={**form, "code_verifier": "wrong"}).json()["error"] == "invalid_grant"
    code = query(client.post("/google/o/oauth2/v2/auth", data={**params, "account": email, "decision": "allow"}))[
        "code"
    ]
    tokens: dict[str, Any] = client.post("/google/token", data={**form, "code": code, "code_verifier": verifier}).json()
    return tokens


def mcp(
    client: TestClient,
    path: str,
    auth: str,
    method: str,
    params: dict[str, Any] | None = None,
    session: str | None = None,
) -> Any:
    headers = {"authorization": auth, "accept": "application/json, text/event-stream"}
    if session:
        headers["mcp-session-id"] = session
    return client.post(
        path, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, headers=headers
    )


def test_google_oauth_refresh_userinfo_and_revoke(client: TestClient) -> None:
    tokens = google_tokens(client)
    assert (
        client.get("/google/userinfo", headers={"authorization": f"Bearer {tokens['access_token']}"}).json()["email"]
        == "alice@acme.test"
    )
    form = {
        "client_id": "local-google-client",
        "client_secret": "local-google-client-secret",
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
    }
    refreshed = client.post("/google/token", data=form).json()
    assert "refresh_token" not in refreshed  # Google keeps the refresh token
    assert client.post("/google/token", data=form).status_code == 200
    assert client.post("/google/revoke", params={"token": tokens["refresh_token"]}).status_code == 200
    assert client.post("/google/token", data=form).json()["error"] == "invalid_grant"
    assert (
        client.get("/google/userinfo", headers={"authorization": f"Bearer {refreshed['access_token']}"}).status_code
        == 401
    )


def test_google_vendor_mcp(client: TestClient) -> None:
    auth = f"Bearer {google_tokens(client)['access_token']}"
    assert mcp(client, "/google/mcp", "Bearer nope", "initialize").status_code == 401
    init = mcp(
        client,
        "/google/mcp",
        auth,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}},
    )
    session = init.headers["mcp-session-id"]
    assert init.json()["result"]["capabilities"] == {"tools": {"listChanged": False}}
    assert mcp(client, "/google/mcp", auth, "tools/list").status_code == 400  # no session
    tools = {
        t["name"]: t for t in mcp(client, "/google/mcp", auth, "tools/list", session=session).json()["result"]["tools"]
    }
    assert tools["sheets_read_range"]["annotations"]["readOnlyHint"] is True
    assert tools["sheets_append_row"]["annotations"]["readOnlyHint"] is False
    called = mcp(
        client,
        "/google/mcp",
        auth,
        "tools/call",
        {"name": "sheets_read_range", "arguments": {"spreadsheet_id": "bug-metrics"}},
        session,
    ).json()
    assert "W41 | 15 | 11" in called["result"]["content"][0]["text"]
    missing = mcp(
        client,
        "/google/mcp",
        auth,
        "tools/call",
        {"name": "sheets_read_range", "arguments": {"spreadsheet_id": "bob-notes"}},
        session,
    ).json()
    assert missing["result"]["isError"] is True  # Bob's sheet is not Alice's
    assert mcp(client, "/google/mcp", auth, "tools/call", {"name": "nope"}, session).json()["error"]["code"] == -32602


def test_custom_mcp_needs_its_api_key(client: TestClient) -> None:
    assert mcp(client, "/mcp/status", "Bearer wrong", "initialize").status_code == 401
    session = mcp(client, "/mcp/status", "Bearer mcp-key-local", "initialize").headers["mcp-session-id"]
    tools = mcp(client, "/mcp/status", "Bearer mcp-key-local", "tools/list", session=session).json()["result"]["tools"]
    restart = next(t for t in tools if t["name"] == "restart_service")
    assert restart["annotations"]["destructiveHint"] is True


def test_jira_create_assign_comment_and_components(client: TestClient) -> None:
    bob = jira_tokens(client, "acc-bob")["access_token"]
    dave = jira_tokens(client, "acc-dave")["access_token"]
    components = jira(client, bob, "project/PAY/components").json()
    assert {c["name"]: (c["lead"] or {}).get("accountId") for c in components} == {
        "Checkout": "acc-bob", "Refunds": "acc-alice", "Webhooks": None,
    }  # fmt: skip
    assert jira(client, bob, "project/SEC/components").status_code == 404  # Bob cannot browse SEC
    fields = {
        "project": {"key": "PAY"},
        "issuetype": {"name": "Bug"},
        "summary": "Checkout fails for carts over 20 items",
        "description": {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Reported in #bugs"}]}]},
        "assignee": {"accountId": "acc-quinn"},
        "labels": ["agent-abc123"],
        "components": [{"name": "Checkout"}],
    }  # fmt: skip
    created = jira(client, bob, "issue", "POST", json={"fields": fields})
    assert created.status_code == 201, created.text
    key = created.json()["key"]
    assert key == "PAY-107"
    issue = jira(client, bob, f"issue/{key}").json()["fields"]
    assert issue["reporter"]["accountId"] == "acc-bob"  # whoever the token belongs to
    assert issue["assignee"]["accountId"] == "acc-quinn"
    assert issue["labels"] == ["agent-abc123"]
    assert issue["components"] == [{"name": "Checkout"}]
    found = jira(client, bob, "search/jql", "POST", json={"jql": 'labels = "agent-abc123"'}).json()["issues"]
    assert [i["key"] for i in found] == [key]
    in_checkout = jira(client, bob, "search/jql", "POST", json={"jql": "project = PAY AND component = Checkout"})
    assert sorted(i["key"] for i in in_checkout.json()["issues"]) == ["PAY-101", key]
    assert jira(client, bob, f"issue/{key}/assignee", "PUT", json={"accountId": "acc-alice"}).status_code == 204
    assert jira(client, bob, f"issue/{key}").json()["fields"]["assignee"]["accountId"] == "acc-alice"
    refused = jira(client, bob, f"issue/{key}/assignee", "PUT", json={"accountId": "acc-dave"})  # other site
    assert refused.status_code == 400
    note = {"body": {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Rolled back v41"}]}]}}  # fmt: skip
    assert jira(client, bob, f"issue/{key}/comment", "POST", json=note).status_code == 201
    comments = jira(client, bob, f"issue/{key}/comment").json()["comments"]
    assert [(c["author"]["accountId"], c["body"]["content"][0]["content"][0]["text"]) for c in comments] == [
        ("acc-bob", "Rolled back v41")
    ]
    # Refusals: no such component, an assignee who cannot be assigned, another site's token, no summary.
    bad = {**fields, "components": [{"name": "Nope"}]}
    assert jira(client, bob, "issue", "POST", json={"fields": bad}).status_code == 400
    bad = {**fields, "assignee": {"accountId": "acc-dave"}}
    assert jira(client, bob, "issue", "POST", json={"fields": bad}).status_code == 400
    assert jira(client, dave, "issue", "POST", json={"fields": fields}).status_code == 404  # wrong site
    bad = {**fields, "summary": " "}
    assert jira(client, bob, "issue", "POST", json={"fields": bad}).status_code == 400
    sec = {**fields, "project": {"key": "SEC"}, "components": [], "assignee": None}
    assert jira(client, bob, "issue", "POST", json={"fields": sec}).status_code == 403


def test_state_saved_by_an_older_stand_in_is_started_afresh(tmp_path: Path) -> None:
    config = Config(state_dir=tmp_path, seed_fixture=tmp_path / "none.json", github_app_public_key=PUBLIC)
    saved = tmp_path / "state.json"
    with TestClient(create_app(config)) as c:
        assert c.post("/_admin/reset").status_code == 200
    state = json.loads(saved.read_text())
    del state["atlassian"]["components"]  # as an earlier version saved it
    state["github"]["installations"]["marker"] = {"kept": True}
    saved.write_text(json.dumps(state))
    with TestClient(create_app(config)):
        pass
    after = json.loads(saved.read_text())
    assert after["atlassian"]["version"] == 3
    assert after["atlassian"]["components"]["PAY"]
    assert "marker" in after["github"]["installations"]  # an up to date provider keeps its state


def test_admin_deletes_created_issues_and_resets_the_seed(client: TestClient) -> None:
    bob = jira_tokens(client, "acc-bob")["access_token"]
    fields = {"project": {"key": "PAY"}, "summary": "Temporary ticket", "issuetype": {"name": "Bug"}}
    key = jira(client, bob, "issue", "POST", json={"fields": fields}).json()["key"]
    (listed,) = client.get("/_admin/atlassian/issues", params={"contains": "temporary"}).json()
    assert (listed["key"], listed["reporter"], listed["assignee"]) == (key, "acc-bob", None)
    jira(client, bob, "issue/PAY-101/assignee", "PUT", json={"accountId": "acc-alice"})
    # Seed issues are never deleted, whatever the phrase.
    assert client.post("/_admin/atlassian/issues/delete", json={"contains": ["ticket", "checkout"]}).json() == {
        "deleted": [key]
    }
    assert client.get("/_admin/atlassian/issues", params={"contains": "temporary"}).json() == []
    assert jira(client, bob, f"issue/{key}").status_code == 404
    assert client.post("/_admin/atlassian/issues/reset").json()["issues"] == 10
    assert jira(client, bob, "issue/PAY-101").json()["fields"]["assignee"]["accountId"] == "acc-bob"
    assert jira(client, bob, "myself").status_code == 200  # the token still works

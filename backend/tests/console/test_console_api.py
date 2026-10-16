"""Console API: identity resolution, permissions, members, roles, settings, audit."""

import uuid
from typing import Any

import httpx
import psycopg
import pytest

from tests.console.conftest import ClerkBackend, SigningKey, auth, q
from tests.stack import FakeTemporal
from tests.world import World

ALICE = ("user_alice", "org_acme")  # Acme admin
BOB = ("user_bob", "org_acme")  # Acme user
DAVE = ("user_dave", "org_globex")  # Globex's only admin


def membership_id(world: World, clerk_org: str, email: str) -> str:
    return str(
        q(
            world,
            "select m.id from app.org_memberships m join app.users u on u.id = m.user_id join app.orgs o on o.id = m.org_id where o.clerk_org_id = %s and u.email = %s",
            (clerk_org, email),
        )
    )


def sql(world: World, statement: str, params: tuple[Any, ...] = ()) -> None:
    with psycopg.connect(world.url) as conn:
        conn.execute(statement, params)


# ------------------------------------------------------------------ authentication and identity


async def test_missing_token(console: httpx.AsyncClient) -> None:
    response = await console.get("/api/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_token"
    assert response.headers["www-authenticate"].startswith("Bearer")


async def test_me_uses_privilege_from_the_database(
    console: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    # Bob's token claims admin; the database says user.
    response = await console.get("/api/me", headers=auth(signing_key, *BOB, rol="admin"))
    assert response.status_code == 200
    body = response.json()
    assert body["membership"]["privilege"] == "user"
    assert body["user"]["email"] == "bob@acme.test"
    assert [r["title"] for r in body["membership"]["roles"]] == ["Engineer"]
    assert (
        await console.get("/api/settings", headers=auth(signing_key, *BOB, rol="admin"))
    ).status_code == 403


@pytest.mark.parametrize(
    ("sub", "org", "code", "status"),
    [
        ("user_alice", "org_globex", "not_a_member", 403),
        ("user_ghost", "org_acme", "not_a_member", 403),
        ("user_alice", None, "no_active_org", 403),
    ],
)
async def test_identity_refusals(
    console: httpx.AsyncClient, signing_key: SigningKey, sub: str, org: str | None, code: str, status: int
) -> None:
    response = await console.get("/api/me", headers=auth(signing_key, sub, org))
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


async def test_invited_disabled_and_inactive_org_are_refused(
    console: httpx.AsyncClient, signing_key: SigningKey, module_world: World
) -> None:
    tag = uuid.uuid4().hex[:6]
    sql(module_world, "select app.identity_upsert_org(%s, 'Paused Co')", (f"org_p{tag}",))
    sql(
        module_world,
        "select app.identity_upsert_membership(%s, 'Paused Co', %s, %s, 'P', 'org:admin')",
        (f"org_p{tag}", f"user_p{tag}", f"p-{tag}@p.test"),
    )
    sql(module_world, "select app.identity_invite('org_acme', %s, 'org:member', null)", (f"p-{tag}@p.test",))
    assert (await console.get("/api/me", headers=auth(signing_key, f"user_p{tag}", "org_acme"))).json()[
        "error"
    ]["code"] == "membership_pending"
    sql(module_world, "update app.orgs set status = 'suspended' where clerk_org_id = %s", (f"org_p{tag}",))
    assert (await console.get("/api/me", headers=auth(signing_key, f"user_p{tag}", f"org_p{tag}"))).json()[
        "error"
    ]["code"] == "org_inactive"
    sql(module_world, "update app.users set status = 'disabled' where clerk_user_id = %s", (f"user_p{tag}",))
    assert (await console.get("/api/me", headers=auth(signing_key, f"user_p{tag}", f"org_p{tag}"))).json()[
        "error"
    ]["code"] == "user_disabled"


async def test_pending_session_is_refused(console: httpx.AsyncClient, signing_key: SigningKey) -> None:
    response = await console.get(
        "/api/me", headers=auth(signing_key, "user_alice", "org_acme", sts="pending")
    )
    assert response.json()["error"]["code"] == "session_pending"


async def test_member_of_two_orgs_switches_by_token(
    console: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    acme = (await console.get("/api/org", headers=auth(signing_key, "user_frank", "org_acme"))).json()
    globex = (await console.get("/api/org", headers=auth(signing_key, "user_frank", "org_globex"))).json()
    assert acme["org"]["name"] == "Acme Corp"
    assert globex["org"]["name"] == "Globex Inc"
    orgs = (await console.get("/api/orgs", headers=auth(signing_key, "user_frank", None))).json()
    assert [o["name"] for o in orgs] == ["Acme Corp", "Globex Inc"]


async def test_cors_allows_only_console_origins(console: httpx.AsyncClient) -> None:
    ok = await console.options(
        "/api/me",
        headers={
            "origin": "http://localhost:5173",
            "access-control-request-method": "GET",
            "access-control-request-headers": "authorization",
        },
    )
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    evil = await console.options(
        "/api/me", headers={"origin": "https://evil.example", "access-control-request-method": "GET"}
    )
    assert "access-control-allow-origin" not in evil.headers


# ------------------------------------------------------------------ org


async def test_org_overview(console: httpx.AsyncClient, signing_key: SigningKey) -> None:
    body = (await console.get("/api/org", headers=auth(signing_key, *BOB))).json()
    assert body["org"]["name"] == "Acme Corp"
    assert body["counts"]["admins"] >= 1
    assert body["workspace"]["external_id"] == "T0ACME"


async def test_rename_goes_to_clerk_first(
    console: httpx.AsyncClient, signing_key: SigningKey, clerk_backend: ClerkBackend, module_world: World
) -> None:
    clerk_backend.fail("PATCH", "/organizations/org_acme", 500)
    response = await console.patch("/api/org", json={"name": "Acme 2"}, headers=auth(signing_key, *ALICE))
    assert response.status_code == 502
    assert q(module_world, "select name from app.orgs where clerk_org_id = 'org_acme'") == "Acme Corp"
    clerk_backend.failures.clear()
    response = await console.patch(
        "/api/org", json={"name": "  Acme Renamed "}, headers=auth(signing_key, *ALICE)
    )
    assert response.json() == {"id": response.json()["id"], "name": "Acme Renamed"}
    assert ("PATCH", "/organizations/org_acme", {"name": "Acme Renamed"}) in clerk_backend.calls
    assert q(module_world, "select name from app.orgs where clerk_org_id = 'org_acme'") == "Acme Renamed"
    await console.patch("/api/org", json={"name": "Acme Corp"}, headers=auth(signing_key, *ALICE))


@pytest.mark.parametrize("body", [{"name": "   "}, {"name": ""}, {"name": "x" * 201}, {}])
async def test_rename_validation(
    console: httpx.AsyncClient, signing_key: SigningKey, body: dict[str, Any]
) -> None:
    response = await console.patch("/api/org", json=body, headers=auth(signing_key, *ALICE))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid"


async def test_non_admin_cannot_rename(console: httpx.AsyncClient, signing_key: SigningKey) -> None:
    response = await console.patch("/api/org", json={"name": "Bob Inc"}, headers=auth(signing_key, *BOB))
    assert response.json()["error"]["code"] == "admin_required"


# ------------------------------------------------------------------ members


async def test_members_list(console: httpx.AsyncClient, signing_key: SigningKey) -> None:
    members = (await console.get("/api/members", headers=auth(signing_key, *BOB))).json()
    by_email = {m["email"]: m for m in members}
    assert by_email["alice@acme.test"]["privilege"] == "admin"
    assert by_email["alice@acme.test"]["roles"][0] == {
        "id": by_email["alice@acme.test"]["roles"][0]["id"],
        "title": "Manager",
        "primary": True,
    }
    assert by_email["carol@acme.test"]["status"] == "invited"
    assert by_email["carol@acme.test"]["signed_up"] is False
    assert by_email["bob@acme.test"]["slack_linked"] is True
    assert "dave@globex.test" not in by_email


async def test_invite_flow(
    console: httpx.AsyncClient, signing_key: SigningKey, clerk_backend: ClerkBackend, module_world: World
) -> None:
    email = f"hire-{uuid.uuid4().hex[:6]}@acme.test"
    response = await console.post(
        "/api/members/invitations",
        json={"email": email, "privilege": "admin"},
        headers=auth(signing_key, *ALICE),
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "invited"
    assert (
        "POST",
        "/organizations/org_acme/invitations",
        {"email_address": email, "role": "org:admin", "inviter_user_id": "user_alice"},
    ) in clerk_backend.calls
    again = await console.post(
        "/api/members/invitations", json={"email": email.upper()}, headers=auth(signing_key, *ALICE)
    )
    assert again.json()["error"]["code"] == "already_invited"
    member = await console.post(
        "/api/members/invitations", json={"email": "bob@acme.test"}, headers=auth(signing_key, *ALICE)
    )
    assert member.json()["error"]["code"] == "already_member"
    assert (
        q(
            module_world,
            "select count(*) from app.audit_log where action = 'member.invited' and target = %s",
            (email,),
        )
        == 1
    )


@pytest.mark.parametrize(("status", "expected"), [(422, 409), (500, 502)])
async def test_clerk_failure_rolls_the_invite_back(
    console: httpx.AsyncClient,
    signing_key: SigningKey,
    clerk_backend: ClerkBackend,
    module_world: World,
    status: int,
    expected: int,
) -> None:
    email = f"fail-{uuid.uuid4().hex[:6]}@acme.test"
    clerk_backend.fail("POST", "/organizations/org_acme/invitations", status, "duplicate_record")
    response = await console.post(
        "/api/members/invitations", json={"email": email}, headers=auth(signing_key, *ALICE)
    )
    assert response.status_code == expected
    assert q(module_world, "select count(*) from app.users where email = %s", (email,)) == 0


@pytest.mark.parametrize(
    "body", [{"email": "nope"}, {"email": "a@b"}, {"email": "x@y.test", "privilege": "owner"}, {}]
)
async def test_invite_validation(
    console: httpx.AsyncClient, signing_key: SigningKey, body: dict[str, Any]
) -> None:
    assert (
        await console.post("/api/members/invitations", json=body, headers=auth(signing_key, *ALICE))
    ).status_code == 422


async def test_revoke_invitation(
    console: httpx.AsyncClient, signing_key: SigningKey, clerk_backend: ClerkBackend, module_world: World
) -> None:
    email = f"rev-{uuid.uuid4().hex[:6]}@acme.test"
    created = (
        await console.post(
            "/api/members/invitations", json={"email": email}, headers=auth(signing_key, *ALICE)
        )
    ).json()
    assert (
        await console.delete(f"/api/members/invitations/{created['id']}", headers=auth(signing_key, *ALICE))
    ).status_code == 204
    assert any(p.endswith("/revoke") for p in clerk_backend.paths("POST"))
    assert (
        q(module_world, "select status from app.org_memberships where id = %s", (created["id"],)) == "removed"
    )
    bob = membership_id(module_world, "org_acme", "bob@acme.test")
    assert (
        await console.delete(f"/api/members/invitations/{bob}", headers=auth(signing_key, *ALICE))
    ).json()["error"]["code"] == "not_invited"


async def test_promote_and_demote(
    console: httpx.AsyncClient, signing_key: SigningKey, clerk_backend: ClerkBackend, module_world: World
) -> None:
    quinn = membership_id(module_world, "org_acme", "quinn@acme.test")
    response = await console.patch(
        f"/api/members/{quinn}", json={"privilege": "admin"}, headers=auth(signing_key, *ALICE)
    )
    assert response.json()["privilege"] == "admin"
    assert (
        "PATCH",
        "/organizations/org_acme/memberships/user_quinn",
        {"role": "org:admin"},
    ) in clerk_backend.calls
    # Quinn is now an admin in our database, so Quinn can use admin routes.
    assert (
        await console.get("/api/settings", headers=auth(signing_key, "user_quinn", "org_acme"))
    ).status_code == 200
    calls = len(clerk_backend.calls)
    same = await console.patch(
        f"/api/members/{quinn}", json={"privilege": "admin"}, headers=auth(signing_key, *ALICE)
    )
    assert same.status_code == 200
    assert len(clerk_backend.calls) == calls  # no change, no Clerk call
    await console.patch(
        f"/api/members/{quinn}", json={"privilege": "user"}, headers=auth(signing_key, *ALICE)
    )
    assert q(module_world, "select privilege from app.org_memberships where id = %s", (quinn,)) == "user"


async def test_last_admin_is_refused_before_clerk_is_called(
    console: httpx.AsyncClient, signing_key: SigningKey, clerk_backend: ClerkBackend, module_world: World
) -> None:
    dave = membership_id(module_world, "org_globex", "dave@globex.test")
    for request in (
        console.patch(f"/api/members/{dave}", json={"privilege": "user"}, headers=auth(signing_key, *DAVE)),
        console.delete(f"/api/members/{dave}", headers=auth(signing_key, *DAVE)),
    ):
        response = await request
        assert response.status_code == 409
        assert response.json()["error"] == {
            "code": "last_admin",
            "message": "An organization must keep at least one active admin.",
            "request_id": response.headers["x-request-id"],
        }
    assert not [p for p in clerk_backend.paths() if "user_dave" in p]
    assert (
        q(module_world, "select privilege || ':' || status from app.org_memberships where id = %s", (dave,))
        == "admin:active"
    )


async def test_clerk_failure_rolls_back_a_demotion(
    console: httpx.AsyncClient, signing_key: SigningKey, clerk_backend: ClerkBackend, module_world: World
) -> None:
    erin = membership_id(module_world, "org_globex", "erin@globex.test")
    clerk_backend.fail("PATCH", "/organizations/org_globex/memberships/user_erin", 500)
    response = await console.patch(
        f"/api/members/{erin}", json={"privilege": "admin"}, headers=auth(signing_key, *DAVE)
    )
    assert response.status_code == 502
    assert q(module_world, "select privilege from app.org_memberships where id = %s", (erin,)) == "user"


async def test_remove_member_cascades_and_locks_them_out(
    console: httpx.AsyncClient,
    signing_key: SigningKey,
    clerk_backend: ClerkBackend,
    module_world: World,
    temporal: FakeTemporal,
) -> None:
    tag = uuid.uuid4().hex[:6]
    sql(
        module_world,
        "select app.identity_upsert_membership('org_acme', 'Acme Corp', %s, %s, 'Temp', 'org:member')",
        (f"user_t{tag}", f"t-{tag}@acme.test"),
    )
    member = membership_id(module_world, "org_acme", f"t-{tag}@acme.test")
    assert (
        await console.get("/api/me", headers=auth(signing_key, f"user_t{tag}", "org_acme"))
    ).status_code == 200
    response = await console.delete(f"/api/members/{member}", headers=auth(signing_key, *ALICE))
    assert response.status_code == 204
    assert ("DELETE", f"/organizations/org_acme/memberships/user_t{tag}", None) in clerk_backend.calls
    assert q(module_world, "select status from app.org_memberships where id = %s", (member,)) == "removed"
    assert (await console.get("/api/me", headers=auth(signing_key, f"user_t{tag}", "org_acme"))).json()[
        "error"
    ]["code"] == "not_a_member"
    # The follow up cancels their running tasks and tells admins about their schedules.
    org = q(module_world, "select id from app.orgs where clerk_org_id = 'org_acme'")
    assert ("MemberRemovedWorkflow", [str(org), str(member)]) in [(w, a) for w, a, _ in temporal.started]


async def test_remove_succeeds_when_clerk_already_dropped_the_member(
    console: httpx.AsyncClient, signing_key: SigningKey, clerk_backend: ClerkBackend, module_world: World
) -> None:
    tag = uuid.uuid4().hex[:6]
    sql(
        module_world,
        "select app.identity_upsert_membership('org_acme', 'Acme Corp', %s, %s, 'Gone', 'org:member')",
        (f"user_g{tag}", f"g-{tag}@acme.test"),
    )
    member = membership_id(module_world, "org_acme", f"g-{tag}@acme.test")
    clerk_backend.fail(
        "DELETE", f"/organizations/org_acme/memberships/user_g{tag}", 404, "resource_not_found"
    )
    assert (
        await console.delete(f"/api/members/{member}", headers=auth(signing_key, *ALICE))
    ).status_code == 204


async def test_another_orgs_member_is_invisible(
    console: httpx.AsyncClient, signing_key: SigningKey, module_world: World
) -> None:
    erin = membership_id(module_world, "org_globex", "erin@globex.test")
    for request in (
        console.patch(f"/api/members/{erin}", json={"privilege": "admin"}, headers=auth(signing_key, *ALICE)),
        console.delete(f"/api/members/{erin}", headers=auth(signing_key, *ALICE)),
        console.put(f"/api/members/{erin}/roles", json={"role_ids": []}, headers=auth(signing_key, *ALICE)),
    ):
        assert (await request).status_code == 404
    assert (
        q(module_world, "select privilege || ':' || status from app.org_memberships where id = %s", (erin,))
        == "user:active"
    )


async def test_non_admin_cannot_manage_members(
    console: httpx.AsyncClient, signing_key: SigningKey, module_world: World
) -> None:
    quinn = membership_id(module_world, "org_acme", "quinn@acme.test")
    for request in (
        console.post(
            "/api/members/invitations", json={"email": "x@acme.test"}, headers=auth(signing_key, *BOB)
        ),
        console.patch(f"/api/members/{quinn}", json={"privilege": "admin"}, headers=auth(signing_key, *BOB)),
        console.delete(f"/api/members/{quinn}", headers=auth(signing_key, *BOB)),
        console.put(f"/api/members/{quinn}/roles", json={"role_ids": []}, headers=auth(signing_key, *BOB)),
    ):
        assert (await request).json()["error"]["code"] == "admin_required"


# ------------------------------------------------------------------ roles


async def test_roles_lifecycle_and_primary_promotion(
    console: httpx.AsyncClient, signing_key: SigningKey, module_world: World
) -> None:
    h = auth(signing_key, *ALICE)
    tag = uuid.uuid4().hex[:4]
    first = (
        await console.post(
            "/api/roles", json={"title": f"On-call {tag}", "description": "Short, action first."}, headers=h
        )
    ).json()
    second = (await console.post("/api/roles", json={"title": f"Reviewer {tag}"}, headers=h)).json()
    dup = await console.post("/api/roles", json={"title": f"ON-CALL {tag}"}, headers=h)
    assert dup.status_code == 409
    quinn = membership_id(module_world, "org_acme", "quinn@acme.test")

    assigned = await console.put(
        f"/api/members/{quinn}/roles",
        json={"role_ids": [first["id"], second["id"]], "primary_role_id": first["id"]},
        headers=h,
    )
    assert [(r["title"], r["primary"]) for r in assigned.json()["roles"]] == [
        (f"On-call {tag}", True),
        (f"Reviewer {tag}", False),
    ]
    switched = await console.put(
        f"/api/members/{quinn}/roles",
        json={"role_ids": [first["id"], second["id"]], "primary_role_id": second["id"]},
        headers=h,
    )
    assert switched.json()["roles"][0] == {"id": second["id"], "title": f"Reviewer {tag}", "primary": True}

    archived = (await console.post(f"/api/roles/{second['id']}/archive", headers=h)).json()
    assert archived["status"] == "archived"
    member = (await console.get("/api/members", headers=h)).json()
    roles = next(m for m in member if m["id"] == quinn)["roles"]
    assert roles == [{"id": first["id"], "title": f"On-call {tag}", "primary": True}]  # promoted
    assert (await console.patch(f"/api/roles/{second['id']}", json={"title": "Again"}, headers=h)).json()[
        "error"
    ]["code"] == "archived"

    renamed = await console.patch(f"/api/roles/{first['id']}", json={"description": "Updated"}, headers=h)
    assert renamed.json()["description"] == "Updated"
    listed = {r["title"]: r for r in (await console.get("/api/roles", headers=h)).json()}
    assert listed[f"On-call {tag}"]["members"] >= 1
    assert f"Reviewer {tag}" not in listed
    assert f"Reviewer {tag}" in {
        r["title"] for r in (await console.get("/api/roles?include_archived=true", headers=h)).json()
    }


@pytest.mark.parametrize(
    "body",
    [
        {"role_ids": ["{a}"], "primary_role_id": None},
        {"role_ids": ["{a}"], "primary_role_id": "{b}"},
        {"role_ids": ["{a}", "{a}"], "primary_role_id": "{a}"},
        {"role_ids": [], "primary_role_id": "{a}"},
    ],
)
async def test_role_assignment_validation(
    console: httpx.AsyncClient, signing_key: SigningKey, module_world: World, body: dict[str, Any]
) -> None:
    roles = {
        r["title"]: r["id"]
        for r in (await console.get("/api/roles", headers=auth(signing_key, *ALICE))).json()
    }
    a, b = roles["Engineer"], roles["QA"]
    rendered = {
        "role_ids": [x.format(a=a, b=b) for x in body["role_ids"]],
        "primary_role_id": body["primary_role_id"].format(a=a, b=b) if body["primary_role_id"] else None,
    }
    quinn = membership_id(module_world, "org_acme", "quinn@acme.test")
    assert (
        await console.put(f"/api/members/{quinn}/roles", json=rendered, headers=auth(signing_key, *ALICE))
    ).status_code == 422


async def test_another_orgs_role_cannot_be_assigned(
    console: httpx.AsyncClient, signing_key: SigningKey, module_world: World
) -> None:
    sre = str(q(module_world, "select id from app.custom_roles where title = 'SRE'"))
    quinn = membership_id(module_world, "org_acme", "quinn@acme.test")
    response = await console.put(
        f"/api/members/{quinn}/roles",
        json={"role_ids": [sre], "primary_role_id": sre},
        headers=auth(signing_key, *ALICE),
    )
    assert response.status_code == 422


async def test_clearing_roles(
    console: httpx.AsyncClient, signing_key: SigningKey, module_world: World
) -> None:
    frank = membership_id(module_world, "org_acme", "frank@both.test")
    response = await console.put(
        f"/api/members/{frank}/roles", json={"role_ids": []}, headers=auth(signing_key, *ALICE)
    )
    assert response.json()["roles"] == []


# ------------------------------------------------------------------ settings and audit


async def test_settings(console: httpx.AsyncClient, signing_key: SigningKey) -> None:
    h = auth(signing_key, *DAVE)
    current = (await console.get("/api/settings", headers=h)).json()
    assert current["allowed_model_tiers"] == ["fast", "standard"]
    updated = await console.patch(
        "/api/settings",
        json={
            "retention_days": 45,
            "max_schedules": 7,
            "allowed_model_tiers": ["advanced", "fast", "fast"],
            "default_model_tier": "advanced",
            "display_settings": {"theme": "dark"},
        },
        headers=h,
    )
    body = updated.json()
    assert (
        body["retention_days"],
        body["max_schedules"],
        body["allowed_model_tiers"],
        body["default_model_tier"],
    ) == (45, 7, ["fast", "advanced"], "advanced")
    assert body["display_settings"] == {"theme": "dark"}
    cleared = (
        await console.patch("/api/settings", json={"clear_user_daily_token_limit": True}, headers=h)
    ).json()
    assert cleared["user_daily_token_limit"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"retention_days": 0},
        {"retention_days": 4000},
        {"max_concurrent_tasks": 0},
        {"allowed_model_tiers": []},
        {"default_model_tier": "standard", "allowed_model_tiers": ["fast"]},
        {"allowed_model_tiers": ["huge"]},
        {"user_daily_token_limit": -5},
        {},
    ],
)
async def test_settings_validation(
    console: httpx.AsyncClient, signing_key: SigningKey, body: dict[str, Any]
) -> None:
    response = await console.patch("/api/settings", json=body, headers=auth(signing_key, *DAVE))
    assert response.status_code == 422, response.text


async def test_settings_are_admin_only(console: httpx.AsyncClient, signing_key: SigningKey) -> None:
    for request in (
        console.get("/api/settings", headers=auth(signing_key, *BOB)),
        console.patch("/api/settings", json={"retention_days": 5}, headers=auth(signing_key, *BOB)),
    ):
        assert (await request).status_code == 403


async def test_audit_log(console: httpx.AsyncClient, signing_key: SigningKey) -> None:
    h = auth(signing_key, *ALICE)
    for _ in range(3):
        await console.post("/api/roles", json={"title": f"Audited {uuid.uuid4().hex[:6]}"}, headers=h)
    page = (await console.get("/api/audit?limit=2", headers=h)).json()
    assert "items" in page, page
    assert len(page["items"]) == 2
    assert page["next_before"]
    newest = page["items"][0]
    assert (newest["action"], newest["actor_type"], newest["actor_email"]) == (
        "role.created",
        "user",
        "alice@acme.test",
    )
    older = (
        await console.get("/api/audit", params={"limit": 2, "before": page["next_before"]}, headers=h)
    ).json()
    assert {i["id"] for i in older["items"]}.isdisjoint({i["id"] for i in page["items"]})
    filtered = (await console.get("/api/audit?action=role.&actor_type=user", headers=h)).json()
    assert filtered["items"]
    assert all(i["action"].startswith("role.") for i in filtered["items"])
    assert (await console.get("/api/audit", headers=auth(signing_key, *BOB))).status_code == 403
    assert (
        await console.get("/api/audit?action=%25", headers=h)
    ).status_code == 200  # wildcards are stripped


async def test_access_requests_listing(console: httpx.AsyncClient, signing_key: SigningKey) -> None:
    rows = (await console.get("/api/access-requests", headers=auth(signing_key, *ALICE))).json()
    assert rows[0]["external_user_id"] == "U0OUTACME"


async def test_unknown_route_has_the_standard_error_shape(console: httpx.AsyncClient) -> None:
    response = await console.get("/api/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"

"""The Clerk stand-in behaves like the parts of Clerk the platform relies on."""

import base64
import hashlib
import hmac
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from fakes.config import Config
from fakes.main import create_app

FIXTURE = Path(__file__).resolve().parents[2] / "backend" / "app" / "seed" / "fixture.json"
SECRET_KEY = {"authorization": "Bearer sk_test_local"}
ORIGIN = "http://localhost:5173"


class Receiver:
    """Captures webhook deliveries; can fail the first N attempts."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.fail_next = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next > 0:
            self.fail_next -= 1
            return httpx.Response(500)
        return httpx.Response(200, json={"status": "applied"})

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
def receiver() -> Receiver:
    return Receiver()


def make_client(tmp_path: Path, receiver: Receiver) -> TestClient:
    config = Config(
        state_dir=tmp_path,
        seed_fixture=FIXTURE,
        clerk_webhook_url="http://receiver/webhooks/clerk",
        console_origins=[ORIGIN],
    )
    app = create_app(config)
    deliverer = app.state.deliverer
    deliverer._http = httpx.AsyncClient(transport=httpx.MockTransport(receiver.handler))
    deliverer.retry_delays = (0.01, 0.01, 0.01)
    return TestClient(app)


@pytest.fixture
def client(tmp_path: Path, receiver: Receiver) -> Iterator[TestClient]:
    with make_client(tmp_path, receiver) as c:
        yield c


def sign_in(client: TestClient, email: str, password: str = "password123") -> tuple[str, dict[str, Any]]:
    response = client.post("/clerk/dev/sign-in", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    state = response.json()
    return state["session_id"], state


def token(client: TestClient, session: str) -> dict[str, Any]:
    raw = client.post(
        "/clerk/dev/session/tokens", headers={"authorization": f"Bearer {session}", "origin": ORIGIN}
    ).json()["jwt"]
    jwks = client.get("/clerk/.well-known/jwks.json").json()
    kid = jwt.get_unverified_header(raw)["kid"]
    key = jwt.PyJWK(next(k for k in jwks["keys"] if k["kid"] == kid)).key
    claims: dict[str, Any] = jwt.decode(
        raw, key, algorithms=["RS256"], issuer="http://localhost:4100/clerk", options={"verify_aud": False}
    )
    return claims


def drain(client: TestClient) -> None:
    assert client.post("/_admin/clerk/webhooks/drain").status_code == 200


def verify_svix(request: httpx.Request, secret: str = "whsec_bG9jYWwtY2xlcmstd2ViaG9vay1zZWNyZXQtMDAwMQ==") -> None:
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = f"{request.headers['svix-id']}.{request.headers['svix-timestamp']}.".encode() + request.content
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    assert request.headers["svix-signature"] == f"v1,{expected}"


# ------------------------------------------------------------------ seed and tokens


def test_seeded_admin_gets_a_v2_token_for_their_org(client: TestClient) -> None:
    session, state = sign_in(client, "alice@acme.test")
    assert state["organizations"] == [{"id": "org_acme", "name": "Acme Corp", "slug": "acme", "role": "org:admin"}]
    assert state["status"] == "active"
    claims = token(client, session)
    assert (claims["sub"], claims["sid"], claims["v"], claims["sts"], claims["azp"]) == (
        "user_alice",
        session,
        2,
        "active",
        ORIGIN,
    )
    assert claims["o"]["id"] == "org_acme"
    assert claims["o"]["rol"] == "admin"
    assert claims["exp"] - claims["iat"] == 60
    assert claims["nbf"] <= claims["iat"]


def test_member_of_two_orgs_switches_active_org(client: TestClient) -> None:
    session, state = sign_in(client, "frank@both.test")
    assert [o["id"] for o in state["organizations"]] == ["org_acme", "org_globex"]
    headers = {"authorization": f"Bearer {session}"}
    assert (
        client.post(
            "/clerk/dev/session/active-org", json={"organization_id": "org_globex"}, headers=headers
        ).status_code
        == 200
    )
    assert token(client, session)["o"]["id"] == "org_globex"
    refused = client.post("/clerk/dev/session/active-org", json={"organization_id": "org_nope"}, headers=headers)
    assert refused.status_code == 403


def test_wrong_password_and_duplicate_signup(client: TestClient) -> None:
    assert client.post("/clerk/dev/sign-in", json={"email": "alice@acme.test", "password": "nope"}).status_code == 422
    dup = client.post("/clerk/dev/sign-up", json={"email": "ALICE@acme.test", "password": "longenough"})
    assert dup.json()["errors"][0]["code"] == "form_identifier_exists"
    assert client.post("/clerk/dev/sign-up", json={"email": "bad", "password": "longenough"}).status_code == 422


def test_ended_sessions_cannot_get_tokens(client: TestClient) -> None:
    session, _ = sign_in(client, "bob@acme.test")
    client.post("/clerk/dev/session/end", headers={"authorization": f"Bearer {session}"})
    assert client.post("/clerk/dev/session/tokens", headers={"authorization": f"Bearer {session}"}).status_code == 401


# ------------------------------------------------------------------ signup, org creation, invitations, webhooks


def test_signup_is_pending_until_an_org_exists(client: TestClient, receiver: Receiver) -> None:
    state = client.post(
        "/clerk/dev/sign-up",
        json={"email": "new@startup.test", "password": "longenough", "first_name": "Nia", "last_name": "New"},
    ).json()
    session = state["session_id"]
    assert state["status"] == "pending"
    pending = token(client, session)
    assert pending["sts"] == "pending"
    assert "o" not in pending
    created = client.post(
        "/clerk/dev/organizations", json={"name": "Startup Co"}, headers={"authorization": f"Bearer {session}"}
    ).json()
    assert created["status"] == "active"
    assert created["organizations"][0]["role"] == "org:admin"
    assert token(client, session)["o"]["rol"] == "admin"
    drain(client)
    assert [e["type"] for e in receiver.events()] == [
        "user.created",
        "organization.created",
        "organizationMembership.created",
    ]
    for request in receiver.requests:
        verify_svix(request)
    user = receiver.events()[0]["data"]
    assert user["email_addresses"][0]["email_address"] == "new@startup.test"
    assert user["primary_email_address_id"] == user["email_addresses"][0]["id"]
    membership = receiver.events()[2]["data"]
    assert (membership["role"], membership["public_user_data"]["identifier"]) == ("org:admin", "new@startup.test")


def test_invited_person_signs_up_and_accepts(client: TestClient, receiver: Receiver) -> None:
    pending = client.get("/clerk/v1/organizations/org_acme/invitations?status=pending", headers=SECRET_KEY).json()
    carol = next(i for i in pending["data"] if i["email_address"] == "carol@acme.test")
    state = client.post("/clerk/dev/sign-up", json={"email": "carol@acme.test", "password": "longenough"}).json()
    assert [i["organization_name"] for i in state["invitations"]] == ["Acme Corp"]
    accepted = client.post(
        f"/clerk/dev/invitations/{carol['id']}/accept", headers={"authorization": f"Bearer {state['session_id']}"}
    ).json()
    assert accepted["active_org_id"] == "org_acme"
    assert accepted["invitations"] == []
    drain(client)
    assert [e["type"] for e in receiver.events()] == [
        "user.created",
        "organizationInvitation.accepted",
        "organizationMembership.created",
    ]


def test_backend_api_requires_the_secret_key(client: TestClient) -> None:
    response = client.get("/clerk/v1/organizations/org_acme")
    assert response.status_code == 401
    assert response.json()["errors"][0]["code"] == "authentication_invalid"


def test_backend_invitation_lifecycle(client: TestClient, receiver: Receiver) -> None:
    created = client.post(
        "/clerk/v1/organizations/org_acme/invitations",
        json={"email_address": "hire@acme.test", "role": "org:member", "inviter_user_id": "user_alice"},
        headers=SECRET_KEY,
    )
    assert created.status_code == 200
    inv = created.json()
    assert (inv["object"], inv["status"], inv["organization_id"]) == ("organization_invitation", "pending", "org_acme")
    dup = client.post(
        "/clerk/v1/organizations/org_acme/invitations",
        json={"email_address": "hire@acme.test", "role": "org:member"},
        headers=SECRET_KEY,
    )
    assert dup.json()["errors"][0]["code"] == "duplicate_record"
    member = client.post(
        "/clerk/v1/organizations/org_acme/invitations",
        json={"email_address": "bob@acme.test", "role": "org:member"},
        headers=SECRET_KEY,
    )
    assert member.json()["errors"][0]["code"] == "already_a_member_in_organization"
    revoked = client.post(
        f"/clerk/v1/organizations/org_acme/invitations/{inv['id']}/revoke",
        json={"requesting_user_id": "user_alice"},
        headers=SECRET_KEY,
    )
    assert revoked.json()["status"] == "revoked"
    drain(client)
    assert [e["type"] for e in receiver.events()] == [
        "organizationInvitation.created",
        "organizationInvitation.revoked",
    ]


def test_backend_membership_and_org_updates(client: TestClient, receiver: Receiver) -> None:
    promoted = client.patch(
        "/clerk/v1/organizations/org_acme/memberships/user_bob", json={"role": "org:admin"}, headers=SECRET_KEY
    )
    assert promoted.json()["role"] == "org:admin"
    assert (
        client.patch(
            "/clerk/v1/organizations/org_acme/memberships/user_bob", json={"role": "owner"}, headers=SECRET_KEY
        ).status_code
        == 422
    )
    assert client.delete("/clerk/v1/organizations/org_acme/memberships/user_bob", headers=SECRET_KEY).status_code == 200
    assert client.delete("/clerk/v1/organizations/org_acme/memberships/user_bob", headers=SECRET_KEY).status_code == 404
    assert (
        client.patch("/clerk/v1/organizations/org_acme", json={"name": "Acme 2"}, headers=SECRET_KEY).json()["name"]
        == "Acme 2"
    )
    members = client.get("/clerk/v1/organizations/org_acme/memberships", headers=SECRET_KEY).json()
    assert "user_bob" not in {m["public_user_data"]["user_id"] for m in members["data"]}
    drain(client)
    assert [e["type"] for e in receiver.events()] == [
        "organizationMembership.updated",
        "organizationMembership.deleted",
        "organization.updated",
    ]


def test_failed_deliveries_are_retried_in_order(client: TestClient, receiver: Receiver) -> None:
    receiver.fail_next = 2
    client.patch("/clerk/v1/organizations/org_acme", json={"name": "Retry Co"}, headers=SECRET_KEY)
    client.patch("/clerk/v1/organizations/org_acme", json={"name": "Retry Co 2"}, headers=SECRET_KEY)
    drain(client)
    log = client.get("/_admin/clerk/webhooks").json()
    assert [len(d["attempts"]) for d in log] == [3, 1]
    assert all(d["delivered"] for d in log)
    assert [e["data"]["name"] for e in receiver.events()[-2:]] == ["Retry Co", "Retry Co 2"]


def test_state_persists_across_restarts_with_stable_keys(tmp_path: Path, receiver: Receiver) -> None:
    with make_client(tmp_path, receiver) as first:
        session, _ = sign_in(first, "alice@acme.test")
        first.post("/clerk/dev/sign-up", json={"email": "persist@x.test", "password": "longenough"})
        raw = first.post(
            "/clerk/dev/session/tokens", headers={"authorization": f"Bearer {session}", "origin": ORIGIN}
        ).json()["jwt"]
    with make_client(tmp_path, receiver) as second:
        assert (
            second.post("/clerk/dev/sign-in", json={"email": "persist@x.test", "password": "longenough"}).status_code
            == 200
        )
        jwks = second.get("/clerk/.well-known/jwks.json").json()
        key = jwt.PyJWK(jwks["keys"][0]).key
        jwt.decode(raw, key, algorithms=["RS256"], options={"verify_aud": False})  # minted before the restart


def test_reset_restores_the_seed(client: TestClient) -> None:
    client.post("/clerk/dev/sign-up", json={"email": "temp@x.test", "password": "longenough"})
    assert client.post("/_admin/reset").json() == {"status": "reset"}
    assert client.post("/clerk/dev/sign-in", json={"email": "temp@x.test", "password": "longenough"}).status_code == 422
    sign_in(client, "alice@acme.test")


def test_reset_keeps_the_signing_key(client: TestClient) -> None:
    before = client.get("/clerk/.well-known/jwks.json").json()
    client.post("/_admin/reset")
    assert client.get("/clerk/.well-known/jwks.json").json() == before

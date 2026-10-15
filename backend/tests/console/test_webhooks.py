"""Clerk webhooks (Svix signed) mirrored into the database."""

import base64
import json
import os
import time
import uuid
from typing import Any

import httpx
import pytest

from app.identity import svix
from tests.console.conftest import q
from tests.world import World

SECRET = "whsec_" + base64.b64encode(b"console-test-webhook-secret-0001").decode()
OLD_SECRET = "whsec_" + base64.b64encode(b"console-test-webhook-secret-0000").decode()


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", f"{SECRET},{OLD_SECRET}")


def signed(
    event: dict[str, Any], secret: str = SECRET, msg_id: str | None = None, timestamp: int | None = None
) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(event).encode()
    msg_id = msg_id or f"msg_{uuid.uuid4().hex}"
    ts = timestamp if timestamp is not None else int(time.time())
    return body, {
        "svix-id": msg_id,
        "svix-timestamp": str(ts),
        "svix-signature": svix.sign(secret, msg_id, ts, body),
        "content-type": "application/json",
    }


async def send(client: httpx.AsyncClient, event: dict[str, Any], **kwargs: Any) -> httpx.Response:
    body, headers = signed(event, **kwargs)
    return await client.post("/webhooks/clerk", content=body, headers=headers)


def user_event(kind: str, user_id: str, email: str, first: str = "Pat", last: str = "Doe") -> dict[str, Any]:
    return {
        "type": kind,
        "object": "event",
        "data": {
            "id": user_id,
            "object": "user",
            "first_name": first,
            "last_name": last,
            "primary_email_address_id": "idn_1",
            "email_addresses": [
                {"id": "idn_0", "email_address": "old@x.test"},
                {"id": "idn_1", "email_address": email},
            ],
        },
    }


def org_event(kind: str, org_id: str, name: str) -> dict[str, Any]:
    return {
        "type": kind,
        "data": {"id": org_id, "object": "organization", "name": name, "slug": name.lower()},
    }


def membership_event(
    kind: str, org_id: str, org_name: str, user_id: str, email: str, role: str
) -> dict[str, Any]:
    return {
        "type": kind,
        "data": {
            "id": f"orgmem_{uuid.uuid4().hex[:6]}",
            "object": "organization_membership",
            "role": role,
            "organization": {"id": org_id, "name": org_name},
            "public_user_data": {
                "user_id": user_id,
                "identifier": email,
                "first_name": "Pat",
                "last_name": "Doe",
            },
        },
    }


def invitation_event(kind: str, org_id: str, email: str, role: str = "org:member") -> dict[str, Any]:
    status = {
        "organizationInvitation.created": "pending",
        "organizationInvitation.revoked": "revoked",
        "organizationInvitation.accepted": "accepted",
    }[kind]
    return {
        "type": kind,
        "data": {
            "id": f"orginv_{uuid.uuid4().hex[:6]}",
            "object": "organization_invitation",
            "email_address": email,
            "role": role,
            "status": status,
            "organization_id": org_id,
        },
    }


def member_state(world: World, clerk_org: str, email: str) -> Any:
    return q(
        world,
        "select m.status, m.privilege from app.org_memberships m join app.users u on u.id = m.user_id join app.orgs o on o.id = m.org_id where o.clerk_org_id = %s and lower(u.email) = lower(%s)",
        (clerk_org, email),
    )


# ------------------------------------------------------------------ signup and invitation flows


async def test_signup_flow_creates_org_and_admin(console: httpx.AsyncClient, module_world: World) -> None:
    tag = uuid.uuid4().hex[:6]
    for event in (
        user_event("user.created", f"user_{tag}", f"founder-{tag}@new.test"),
        org_event("organization.created", f"org_{tag}", f"Startup {tag}"),
        membership_event(
            "organizationMembership.created",
            f"org_{tag}",
            f"Startup {tag}",
            f"user_{tag}",
            f"founder-{tag}@new.test",
            "org:admin",
        ),
    ):
        response = await send(console, event)
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "applied"}
    assert member_state(module_world, f"org_{tag}", f"founder-{tag}@new.test") == ("active", "admin")
    assert (
        q(module_world, "select name from app.users where clerk_user_id = %s", (f"user_{tag}",)) == "Pat Doe"
    )


async def test_invitation_then_signup_links_the_same_person(
    console: httpx.AsyncClient, module_world: World
) -> None:
    email = f"invitee-{uuid.uuid4().hex[:6]}@acme.test"
    assert (
        await send(console, invitation_event("organizationInvitation.created", "org_acme", email))
    ).status_code == 200
    assert member_state(module_world, "org_acme", email) == ("invited", "user")
    clerk_id = f"user_{uuid.uuid4().hex[:6]}"
    await send(console, user_event("user.created", clerk_id, email))
    await send(console, invitation_event("organizationInvitation.accepted", "org_acme", email))
    await send(
        console,
        membership_event(
            "organizationMembership.created", "org_acme", "Acme Corp", clerk_id, email, "org:member"
        ),
    )
    assert member_state(module_world, "org_acme", email) == ("active", "user")
    assert q(module_world, "select count(*) from app.users where lower(email) = lower(%s)", (email,)) == 1


async def test_membership_before_user_event_still_works(
    console: httpx.AsyncClient, module_world: World
) -> None:
    tag = uuid.uuid4().hex[:6]
    response = await send(
        console,
        membership_event(
            "organizationMembership.created",
            "org_acme",
            "Acme Corp",
            f"user_{tag}",
            f"early-{tag}@acme.test",
            "org:member",
        ),
    )
    assert response.status_code == 200
    await send(console, user_event("user.created", f"user_{tag}", f"early-{tag}@acme.test", first="Early"))
    assert member_state(module_world, "org_acme", f"early-{tag}@acme.test") == ("active", "user")


async def test_role_change_removal_revocation_and_deletions(
    console: httpx.AsyncClient, module_world: World
) -> None:
    tag = uuid.uuid4().hex[:6]
    email, user = f"m-{tag}@acme.test", f"user_{tag}"
    await send(
        console,
        membership_event(
            "organizationMembership.created", "org_acme", "Acme Corp", user, email, "org:member"
        ),
    )
    await send(
        console,
        membership_event("organizationMembership.updated", "org_acme", "Acme Corp", user, email, "org:admin"),
    )
    assert member_state(module_world, "org_acme", email) == ("active", "admin")
    await send(
        console,
        membership_event("organizationMembership.deleted", "org_acme", "Acme Corp", user, email, "org:admin"),
    )
    assert member_state(module_world, "org_acme", email)[0] == "removed"

    invited = f"inv-{tag}@acme.test"
    await send(console, invitation_event("organizationInvitation.created", "org_acme", invited))
    await send(console, invitation_event("organizationInvitation.revoked", "org_acme", invited))
    assert member_state(module_world, "org_acme", invited)[0] == "removed"

    await send(
        console,
        membership_event(
            "organizationMembership.created",
            "org_acme",
            "Acme Corp",
            f"user_x{tag}",
            f"x-{tag}@acme.test",
            "org:member",
        ),
    )
    await send(
        console, {"type": "user.deleted", "data": {"id": f"user_x{tag}", "object": "user", "deleted": True}}
    )
    assert (
        q(module_world, "select status from app.users where clerk_user_id = %s", (f"user_x{tag}",))
        == "disabled"
    )

    await send(console, org_event("organization.created", f"org_gone{tag}", "Gone"))
    await send(
        console,
        {
            "type": "organization.deleted",
            "data": {"id": f"org_gone{tag}", "object": "organization", "deleted": True},
        },
    )
    assert (
        q(module_world, "select status from app.orgs where clerk_org_id = %s", (f"org_gone{tag}",))
        == "offboarding"
    )


async def test_unknown_events_are_acknowledged(console: httpx.AsyncClient) -> None:
    response = await send(console, {"type": "session.created", "data": {"id": "sess_1"}})
    assert response.json() == {"status": "ignored"}


async def test_last_admin_demotion_via_webhook_is_refused(
    console: httpx.AsyncClient, module_world: World
) -> None:
    response = await send(
        console,
        membership_event(
            "organizationMembership.updated",
            "org_globex",
            "Globex Inc",
            "user_dave",
            "dave@globex.test",
            "org:member",
        ),
    )
    assert response.status_code == 500  # Svix retries; an admin must fix it in Clerk
    assert member_state(module_world, "org_globex", "dave@globex.test") == ("active", "admin")


# ------------------------------------------------------------------ signatures


async def test_signature_checks(console: httpx.AsyncClient) -> None:
    event = org_event("organization.updated", "org_acme", "Acme Corp")
    body, headers = signed(
        event, secret="whsec_" + base64.b64encode(b"some-other-secret-xxxxxxxxxxxx").decode()
    )
    assert (await console.post("/webhooks/clerk", content=body, headers=headers)).status_code == 400

    body, headers = signed(event, timestamp=int(time.time()) - 600)
    assert (await console.post("/webhooks/clerk", content=body, headers=headers)).status_code == 400

    body, headers = signed(event)
    for missing in ("svix-id", "svix-timestamp", "svix-signature"):
        partial = {k: v for k, v in headers.items() if k != missing}
        assert (await console.post("/webhooks/clerk", content=body, headers=partial)).status_code == 400

    body, headers = signed(event)
    assert (
        await console.post("/webhooks/clerk", content=body + b" ", headers=headers)
    ).status_code == 400  # body changed


async def test_rotated_secrets_and_multiple_signatures(console: httpx.AsyncClient) -> None:
    event = org_event("organization.updated", "org_acme", "Acme Corp")
    assert (await send(console, event, secret=OLD_SECRET)).status_code == 200
    body, headers = signed(event)
    headers["svix-signature"] = "v1,bm90LXRoZS1zaWduYXR1cmU= " + headers["svix-signature"]
    assert (await console.post("/webhooks/clerk", content=body, headers=headers)).status_code == 200


async def test_invalid_json_is_rejected(console: httpx.AsyncClient) -> None:
    body = b"{not json"
    msg_id, ts = "msg_bad", int(time.time())
    headers = {
        "svix-id": msg_id,
        "svix-timestamp": str(ts),
        "svix-signature": svix.sign(SECRET, msg_id, ts, body),
    }
    assert (await console.post("/webhooks/clerk", content=body, headers=headers)).status_code == 400


# ------------------------------------------------------------------ dedupe and retries


async def test_duplicate_deliveries_are_applied_once(console: httpx.AsyncClient, module_world: World) -> None:
    if not os.environ.get("CI") and not await _kv_up():
        pytest.skip("Key Value not reachable")
    email = f"dup-{uuid.uuid4().hex[:6]}@acme.test"
    msg_id = f"msg_{uuid.uuid4().hex}"
    event = invitation_event("organizationInvitation.created", "org_acme", email)
    assert (await send(console, event, msg_id=msg_id)).json() == {"status": "applied"}
    # An admin revokes in between; a replay of the old delivery must not resurrect the invitation.
    await send(console, invitation_event("organizationInvitation.revoked", "org_acme", email))
    assert (await send(console, event, msg_id=msg_id)).json() == {"status": "duplicate"}
    assert member_state(module_world, "org_acme", email)[0] == "removed"


async def test_failed_delivery_can_be_retried(console: httpx.AsyncClient, module_world: World) -> None:
    tag = uuid.uuid4().hex[:6]
    msg_id = f"msg_{uuid.uuid4().hex}"
    event = invitation_event("organizationInvitation.created", f"org_late{tag}", f"late-{tag}@x.test")
    assert (await send(console, event, msg_id=msg_id)).status_code == 500  # org not known yet
    await send(console, org_event("organization.created", f"org_late{tag}", "Late"))
    assert (await send(console, event, msg_id=msg_id)).json() == {"status": "applied"}  # claim was released
    assert member_state(module_world, f"org_late{tag}", f"late-{tag}@x.test") == ("invited", "user")


async def _kv_up() -> bool:
    from app import kv

    client = kv.make_client("redis://localhost:56379/14")
    try:
        return await kv.ping(client)
    except Exception:
        return False
    finally:
        await client.aclose()


async def test_a_late_event_does_not_resurrect_a_revoked_invitation(
    console: httpx.AsyncClient, module_world: World
) -> None:
    email = f"late-{uuid.uuid4().hex[:6]}@acme.test"
    created = invitation_event("organizationInvitation.created", "org_acme", email)
    created["timestamp"] = int(time.time() * 1000) - 5_000
    revoked = invitation_event("organizationInvitation.revoked", "org_acme", email)
    revoked["timestamp"] = int(time.time() * 1000)
    await send(console, created)
    await send(console, revoked)
    # Svix delivers the creation again (a retry with a new message id), after the revocation.
    assert (await send(console, created)).status_code == 200
    assert member_state(module_world, "org_acme", email)[0] == "removed"


async def test_oversized_payloads_are_refused_before_verification(console: httpx.AsyncClient) -> None:
    body, headers = signed({"type": "user.created", "data": {"padding": "x" * (1024 * 1024 + 10)}})
    response = await console.post("/webhooks/clerk", content=body, headers=headers)
    assert response.status_code == 413

"""Clerk events mirrored into our database through the identity functions (migration 002).

Called with a connection logged in as ``identity_service``; every function is idempotent, so
Svix retries and the synchronous console mirroring are both safe.
"""

import logging
from typing import Any

from psycopg import AsyncConnection

log = logging.getLogger(__name__)


def primary_email(user: dict[str, Any]) -> str | None:
    addresses = user.get("email_addresses") or []
    primary = user.get("primary_email_address_id")
    for address in addresses:
        if address.get("id") == primary:
            return str(address.get("email_address"))
    return str(addresses[0]["email_address"]) if addresses else None


def full_name(first: Any, last: Any) -> str | None:
    name = " ".join(part for part in (first, last) if part)
    return name or None


async def apply_event(conn: AsyncConnection, event: dict[str, Any]) -> str:
    """Apply one event. Returns ``applied`` or ``ignored``."""
    kind, data = event.get("type"), event.get("data") or {}

    async def call(sql: str, *args: Any) -> None:
        await conn.execute(sql, args)

    if kind in ("user.created", "user.updated"):
        email = primary_email(data)
        if not email:
            return "ignored"  # we only support users with an email address
        await call(
            "select app.identity_upsert_user(%s, %s, %s)",
            data["id"],
            email,
            full_name(data.get("first_name"), data.get("last_name")),
        )
    elif kind == "user.deleted":
        await call("select app.identity_disable_user(%s)", data["id"])
    elif kind in ("organization.created", "organization.updated"):
        await call("select app.identity_upsert_org(%s, %s)", data["id"], data.get("name"))
    elif kind == "organization.deleted":
        await call("select app.identity_offboard_org(%s)", data["id"])
    elif kind in ("organizationMembership.created", "organizationMembership.updated"):
        org, person = data["organization"], data["public_user_data"]
        await call(
            "select app.identity_upsert_membership(%s, %s, %s, %s, %s, %s)",
            org["id"], org.get("name"), person["user_id"], person["identifier"],
            full_name(person.get("first_name"), person.get("last_name")), data["role"],
        )  # fmt: skip
    elif kind == "organizationMembership.deleted":
        await call(
            "select app.identity_remove_membership(%s, %s)",
            data["organization"]["id"],
            data["public_user_data"]["user_id"],
        )
    elif kind == "organizationInvitation.created":
        await call(
            "select app.identity_invite(%s, %s, %s, %s)",
            data["organization_id"],
            data["email_address"],
            data["role"],
            None,
        )
    elif kind == "organizationInvitation.revoked":
        await call(
            "select app.identity_revoke_invitation(%s, %s)", data["organization_id"], data["email_address"]
        )
    else:
        # organizationInvitation.accepted is followed by organizationMembership.created.
        return "ignored"
    return "applied"

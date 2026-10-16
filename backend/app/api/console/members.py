"""Console: members, invitations, privileges, removal, role assignment.

Identity changes (invite, revoke, promote, demote, remove) follow one pattern: apply the change in
an identity_service transaction (the database's last admin guard can refuse it there, before
Clerk is touched), call Clerk, then commit. A Clerk failure rolls the database change back.
"""

import re
from typing import Any, Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from app.api.console.common import audit, clerk, fetch_all, fetch_one, identity_tx
from app.api.errors import ApiError
from app.api.temporal_client import temporal_client
from app.auth.console import ConsoleIdentity, console_identity, require_admin, resources
from app.db.uow import UnitOfWork, get_uow
from app.identity.clerk_api import ClerkError
from app.identity.invite import CLERK_ROLE, InviteError, invite_member
from app.schedules.temporal import start_member_removed
from app.settings import get_settings

router = APIRouter(prefix="/api/members")

Privilege = Literal["admin", "user"]
LAST_ADMIN = ApiError(409, "last_admin", "An organization must keep at least one active admin.")

MEMBERS_SQL = """
select m.id, u.email, u.name, m.privilege, m.status, m.joined_at, m.created_at,
       (u.clerk_user_id is not null) as signed_up,
       exists (select 1 from app.platform_identities pi where pi.membership_id = m.id) as slack_linked,
       coalesce(json_agg(json_build_object('id', r.id, 'title', r.title, 'primary', mr.is_primary)
                         order by mr.is_primary desc, r.title) filter (where r.id is not null), '[]') as roles
  from app.org_memberships m
  join app.users u on u.id = m.user_id
  left join app.membership_roles mr on mr.membership_id = m.id
  left join app.custom_roles r on r.id = mr.role_id and r.status = 'active'
 where {where}
 group by m.id, u.id
 order by m.status = 'invited', lower(u.email)
"""


async def _member(conn: Any, membership_id: UUID) -> dict[str, Any]:
    rows = await fetch_all(conn, MEMBERS_SQL.format(where="m.id = %s"), (membership_id,))
    if not rows:
        raise ApiError(404, "not_found", "No such member.")
    return rows[0]


async def _target(conn: Any, membership_id: UUID) -> dict[str, Any]:
    """The membership (in the bound org, RLS) with what identity calls need."""
    row = await fetch_one(
        conn,
        "select m.id, m.status, m.privilege, u.email, u.clerk_user_id from app.org_memberships m "
        "join app.users u on u.id = m.user_id where m.id = %s",
        (membership_id,),
    )
    if row is None or row["status"] == "removed":
        raise ApiError(404, "not_found", "No such member.")
    return row


@router.get("")
async def list_members(
    identity: ConsoleIdentity = Depends(console_identity),
    uow: UnitOfWork = Depends(get_uow),
    include_removed: bool = False,
) -> list[dict[str, Any]]:
    where = "true" if include_removed and identity.is_admin else "m.status <> 'removed'"
    return await fetch_all(uow.connection, MEMBERS_SQL.format(where=where))


EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Invite(BaseModel):
    email: str = Field(max_length=254)
    privilege: Privilege = "user"

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        # Deliberately permissive (internal and reserved domains are fine); Clerk sends the email.
        value = value.strip()
        if not EMAIL.match(value):
            raise ValueError("not a valid email address")
        return value


@router.post("/invitations", status_code=201)
async def invite(
    body: Invite,
    request: Request,
    identity: ConsoleIdentity = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    email = str(body.email)
    existing = await fetch_one(
        uow.connection,
        "select m.status from app.org_memberships m join app.users u on u.id = m.user_id "
        "where lower(u.email) = lower(%s)",
        (email,),
    )
    if existing and existing["status"] == "active":
        raise ApiError(409, "already_member", "This person is already a member.")
    if existing and existing["status"] == "invited":
        raise ApiError(409, "already_invited", "This person already has a pending invitation.")
    pool = resources(request).identity_pool
    if pool is None:
        raise ApiError(503, "identity_unavailable", "Identity management is not available.")
    try:
        membership_id = await invite_member(
            pool, clerk(request), identity.clerk_org_id, email, body.privilege, identity.clerk_user_id
        )
    except InviteError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc
    await audit(uow.connection, identity, "member.invited", email, {"privilege": body.privilege})
    return await _member(uow.connection, membership_id)


@router.delete("/invitations/{membership_id}", status_code=204)
async def revoke_invitation(
    membership_id: UUID,
    request: Request,
    identity: ConsoleIdentity = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> None:
    target = await _target(uow.connection, membership_id)
    if target["status"] != "invited":
        raise ApiError(409, "not_invited", "This member has already joined; remove them instead.")
    await _revoke(request, identity, target["email"])
    await audit(uow.connection, identity, "member.invitation_revoked", target["email"])


async def _revoke(request: Request, identity: ConsoleIdentity, email: str) -> None:
    async with identity_tx(request) as conn:
        await conn.execute("select app.identity_revoke_invitation(%s, %s)", (identity.clerk_org_id, email))
        try:
            pending = await clerk(request).list_invitations(identity.clerk_org_id)
            for invitation in pending:
                if invitation.get("email_address", "").lower() == email.lower():
                    await clerk(request).revoke_invitation(
                        identity.clerk_org_id, invitation["id"], identity.clerk_user_id
                    )
        except ClerkError as exc:
            raise ApiError(502, "clerk_failed", "Could not revoke the invitation. Try again.") from exc


class PrivilegeChange(BaseModel):
    privilege: Privilege


@router.patch("/{membership_id}")
async def change_privilege(
    membership_id: UUID,
    body: PrivilegeChange,
    request: Request,
    identity: ConsoleIdentity = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    target = await _target(uow.connection, membership_id)
    if target["status"] != "active" or not target["clerk_user_id"]:
        raise ApiError(409, "not_active", "Only members who have joined can be promoted or demoted.")
    if target["privilege"] == body.privilege:
        return await _member(uow.connection, membership_id)
    try:
        async with identity_tx(request) as conn:
            await conn.execute(
                "select app.identity_set_privilege(%s, %s, %s)",
                (identity.clerk_org_id, target["clerk_user_id"], CLERK_ROLE[body.privilege]),
            )
            try:
                await clerk(request).update_membership_role(
                    identity.clerk_org_id, target["clerk_user_id"], CLERK_ROLE[body.privilege]
                )
            except ClerkError as exc:
                raise ApiError(
                    502, "clerk_failed", "Could not change the role in the identity provider."
                ) from exc
    except psycopg.errors.CheckViolation as exc:
        raise LAST_ADMIN from exc
    await audit(
        uow.connection,
        identity,
        "member.privilege_changed",
        target["email"],
        {"from": target["privilege"], "to": body.privilege},
    )
    return await _member(uow.connection, membership_id)


@router.delete("/{membership_id}", status_code=204)
async def remove_member(
    membership_id: UUID,
    request: Request,
    identity: ConsoleIdentity = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> None:
    target = await _target(uow.connection, membership_id)
    if target["status"] == "invited":
        await _revoke(request, identity, target["email"])
        await audit(uow.connection, identity, "member.invitation_revoked", target["email"])
        return
    try:
        async with identity_tx(request) as conn:
            await conn.execute(
                "select app.identity_remove_membership(%s, %s)",
                (identity.clerk_org_id, target["clerk_user_id"]),
            )
            try:
                await clerk(request).delete_membership(identity.clerk_org_id, target["clerk_user_id"])
            except ClerkError as exc:
                if exc.status != 404:  # already gone in Clerk: nothing to undo
                    raise ApiError(
                        502, "clerk_failed", "Could not remove the member in the identity provider."
                    ) from exc
    except psycopg.errors.CheckViolation as exc:
        raise LAST_ADMIN from exc
    # Removal trigger: linked accounts revoked, owned schedules paused, sessions closed. The follow-up
    # workflow cancels their running tasks and tells admins about the paused schedules.
    await audit(uow.connection, identity, "member.removed", target["email"])
    await start_member_removed(await temporal_client(request), get_settings(), identity.org_id, membership_id)


class RoleAssignment(BaseModel):
    role_ids: list[UUID] = Field(default_factory=list, max_length=20)
    primary_role_id: UUID | None = None

    @model_validator(mode="after")
    def _primary_among_roles(self) -> "RoleAssignment":
        if len(set(self.role_ids)) != len(self.role_ids):
            raise ValueError("role_ids must be unique")
        if self.role_ids and self.primary_role_id not in self.role_ids:
            raise ValueError("primary_role_id must be one of role_ids")
        if not self.role_ids and self.primary_role_id is not None:
            raise ValueError("primary_role_id needs role_ids")
        return self


@router.put("/{membership_id}/roles")
async def assign_roles(
    membership_id: UUID,
    body: RoleAssignment,
    identity: ConsoleIdentity = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    conn = uow.connection
    target = await _target(conn, membership_id)
    if body.role_ids:
        found = await fetch_all(
            conn, "select id from app.custom_roles where id = any(%s) and status = 'active'", (body.role_ids,)
        )
        if len(found) != len(body.role_ids):
            raise ApiError(422, "invalid", "Every role must exist and be active.")
    # The "exactly one primary" rule is checked at commit, so the order below is safe.
    await conn.execute(
        "update app.membership_roles set is_primary = false where membership_id = %s", (membership_id,)
    )
    await conn.execute(
        "delete from app.membership_roles where membership_id = %s and not (role_id = any(%s))",
        (membership_id, body.role_ids),
    )
    for role_id in body.role_ids:
        await conn.execute(
            "insert into app.membership_roles (org_id, membership_id, role_id, is_primary) "
            "values (%s, %s, %s, %s) "
            "on conflict (membership_id, role_id) do update set is_primary = excluded.is_primary",
            (identity.org_id, membership_id, role_id, role_id == body.primary_role_id),
        )
    await audit(
        conn,
        identity,
        "member.roles_assigned",
        target["email"],
        {
            "roles": [str(r) for r in body.role_ids],
            "primary": str(body.primary_role_id) if body.primary_role_id else None,
        },
    )
    return await _member(conn, membership_id)

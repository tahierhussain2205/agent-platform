"""Console identity: a verified Clerk session mapped to our membership (spec 14.3).

Privilege comes from our database (``app.resolve_console_identity``), never from the token.
The resolved org binds the request's unit of work, so every query that follows is scoped to it.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import Depends, Request

from app.auth.clerk import AuthError, VerifiedSession
from app.db.uow import UnitOfWork, get_uow

if TYPE_CHECKING:
    from app.api.main import Resources


@dataclass(frozen=True)
class ConsoleIdentity:
    org_id: UUID
    membership_id: UUID
    user_id: UUID
    privilege: str
    clerk_user_id: str
    clerk_org_id: str

    @property
    def is_admin(self) -> bool:
        return self.privilege == "admin"


def resources(request: Request) -> "Resources":
    res: Resources = request.app.state.resources
    return res


def bearer_token(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError(401, "missing_token", "Sign in to continue.")
    return token.strip()


async def verified_session(request: Request) -> VerifiedSession:
    verifier = resources(request).clerk_verifier
    if verifier is None:
        raise AuthError(503, "auth_not_configured", "Sign in is not available.")
    return await verifier.verify(bearer_token(request))


async def console_identity(
    request: Request,
    session: VerifiedSession = Depends(verified_session),
    uow: UnitOfWork = Depends(get_uow),
) -> ConsoleIdentity:
    if not session.clerk_org_id:
        raise AuthError(403, "no_active_org", "Choose an organization first.")
    pool = resources(request).db_pool
    if pool is None:
        raise AuthError(503, "auth_not_configured", "Sign in is not available.")
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "select org_id, membership_id, user_id, privilege, membership_status, user_status, "
                "org_status from app.resolve_console_identity(%s, %s)",
                (session.clerk_user_id, session.clerk_org_id),
            )
        ).fetchone()
    if row is None:
        raise AuthError(403, "not_a_member", "You are not a member of this organization.")
    org_id, membership_id, user_id, privilege, membership_status, user_status, org_status = row
    if user_status != "active":
        raise AuthError(403, "user_disabled", "Your account is disabled.")
    if org_status != "active":
        raise AuthError(403, "org_inactive", "This organization is not active.")
    if membership_status == "invited":
        raise AuthError(403, "membership_pending", "Accept your invitation to continue.")
    if membership_status != "active":
        raise AuthError(403, "not_a_member", "You are not a member of this organization.")
    await uow.bind(org_id)
    identity = ConsoleIdentity(
        org_id, membership_id, user_id, privilege, session.clerk_user_id, session.clerk_org_id
    )
    request.state.identity = identity
    return identity


async def require_admin(identity: ConsoleIdentity = Depends(console_identity)) -> ConsoleIdentity:
    if not identity.is_admin:
        raise AuthError(403, "admin_required", "Only admins can do this.")
    return identity

"""Inviting someone: mirrored into our database and sent through Clerk in one identity
transaction (decision D-056). Used by the console and by Slack access requests."""

from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from app.identity.clerk_api import ClerkClient, ClerkError

CLERK_ROLE = {"admin": "org:admin", "user": "org:member"}


class InviteError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


async def invite_member(
    identity_pool: AsyncConnectionPool,
    clerk: ClerkClient,
    clerk_org_id: str,
    email: str,
    privilege: str,
    inviter_clerk_user_id: str | None,
) -> UUID:
    """Returns the membership id. Raises InviteError (409 refused by Clerk, 502 Clerk down)."""
    async with identity_pool.connection() as conn:
        cur = await conn.execute(
            "select app.identity_invite(%s, %s, %s, %s)",
            (clerk_org_id, email, CLERK_ROLE[privilege], inviter_clerk_user_id),
        )
        row = await cur.fetchone()
        assert row is not None
        try:
            await clerk.create_invitation(clerk_org_id, email, CLERK_ROLE[privilege], inviter_clerk_user_id)
        except ClerkError as exc:
            if exc.status in (400, 409, 422):
                raise InviteError(
                    409, "invitation_refused", f"The identity provider refused the invitation: {exc.message}"
                ) from exc
            raise InviteError(502, "clerk_failed", "Could not send the invitation. Try again.") from exc
        membership_id: UUID = row[0]
    return membership_id

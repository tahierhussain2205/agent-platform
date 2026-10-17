"""Console: the autonomy ceiling (spec 4.5): the most the org lets its people work in Auto. Without one
every task runs in Ask; people choose Ask or Auto within it, in the agent's Home tab in Slack.
Irreversible actions need approval in either mode, and a repository's protected paths still ask."""

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.console.common import audit, fetch_one
from app.auth.console import ConsoleIdentity, console_identity, require_admin
from app.db.uow import UnitOfWork, get_uow

router = APIRouter(prefix="/api")


class AutonomyUpdate(BaseModel):
    org: Literal["ask", "auto"]


@router.get("/autonomy")
async def get_autonomy(
    identity: ConsoleIdentity = Depends(console_identity), uow: UnitOfWork = Depends(get_uow)
) -> dict[str, Any]:
    row = await fetch_one(
        uow.connection,
        "select max_mode::text as max_mode from app.autonomy_policies where scope_type = 'org'",
    )
    return {"org": row["max_mode"] if row else "ask", "set": row is not None}


@router.put("/autonomy")
async def set_autonomy(
    body: AutonomyUpdate,
    identity: ConsoleIdentity = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    await uow.connection.execute(
        "insert into app.autonomy_policies (org_id, scope_type, max_mode, updated_by) "
        "values (%s, 'org', %s, %s) on conflict (org_id) where scope_type = 'org' "
        "do update set max_mode = excluded.max_mode, updated_by = excluded.updated_by, updated_at = now()",
        (identity.org_id, body.org, identity.membership_id),
    )
    await audit(uow.connection, identity, "autonomy.updated", "org", {"max_mode": body.org})
    return await get_autonomy(identity, uow)

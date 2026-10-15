"""Console: current user, org overview, settings, audit, access requests."""

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator, model_validator

from app.api.console.common import audit, clerk, fetch_all, fetch_one
from app.api.errors import ApiError
from app.auth.clerk import VerifiedSession
from app.auth.console import ConsoleIdentity, console_identity, require_admin, resources, verified_session
from app.db.uow import UnitOfWork, get_uow
from app.identity.clerk_api import ClerkError

router = APIRouter(prefix="/api")

Tier = Literal["fast", "standard", "advanced"]


@router.get("/me")
async def me(
    identity: ConsoleIdentity = Depends(console_identity), uow: UnitOfWork = Depends(get_uow)
) -> dict[str, Any]:
    conn = uow.connection
    user = await fetch_one(conn, "select id, email, name from app.users where id = %s", (identity.user_id,))
    org = await fetch_one(conn, "select id, name from app.orgs where id = %s", (identity.org_id,))
    roles = await fetch_all(
        conn,
        "select r.id, r.title, mr.is_primary from app.membership_roles mr "
        "join app.custom_roles r on r.id = mr.role_id "
        "where mr.membership_id = %s and r.status = 'active' order by mr.is_primary desc, r.title",
        (identity.membership_id,),
    )
    return {
        "user": user,
        "org": org,
        "membership": {"id": identity.membership_id, "privilege": identity.privilege, "roles": roles},
    }


@router.get("/orgs")
async def my_orgs(
    request: Request, session: VerifiedSession = Depends(verified_session)
) -> list[dict[str, Any]]:
    """Orgs the signed in user belongs to (no active org needed). Used by the org switcher."""
    pool = resources(request).db_pool
    if pool is None:
        raise ApiError(503, "unavailable", "The database is unavailable.")
    async with pool.connection() as conn:
        return await fetch_all(
            conn,
            "select org_id as id, clerk_org_id, name, privilege from app.list_console_orgs(%s)",
            (session.clerk_user_id,),
        )


@router.get("/org")
async def org_overview(
    identity: ConsoleIdentity = Depends(console_identity), uow: UnitOfWork = Depends(get_uow)
) -> dict[str, Any]:
    conn = uow.connection
    org = await fetch_one(
        conn,
        "select id, name, clerk_org_id, status, plan, region, created_at from app.orgs where id = %s",
        (identity.org_id,),
    )
    counts = await fetch_one(
        conn,
        """
        select
          (select count(*) from app.org_memberships where status = 'active') as members,
          (select count(*) from app.org_memberships
            where status = 'active' and privilege = 'admin') as admins,
          (select count(*) from app.org_memberships where status = 'invited') as invited,
          (select count(*) from app.custom_roles where status = 'active') as roles,
          (select count(*) from app.channels where is_allowlisted and not archived) as allowlisted_channels,
          (select count(*) from app.schedules where status = 'active') as active_schedules
        """,
    )
    workspace = await fetch_one(
        conn, "select name, external_id, status from app.workspaces where status = 'active' limit 1"
    )
    return {"org": org, "counts": counts, "workspace": workspace}


class OrgUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value.strip()


@router.patch("/org")
async def rename_org(
    body: OrgUpdate,
    request: Request,
    identity: ConsoleIdentity = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    try:
        await clerk(request).update_organization(identity.clerk_org_id, body.name)
    except ClerkError as exc:
        raise ApiError(
            502, "clerk_failed", "Could not rename the organization in the identity provider."
        ) from exc
    await uow.connection.execute("update app.orgs set name = %s where id = %s", (body.name, identity.org_id))
    await audit(uow.connection, identity, "org.renamed", body.name)
    return {"id": identity.org_id, "name": body.name}


# ------------------------------------------------------------------ settings


class SettingsUpdate(BaseModel):
    default_model_tier: Tier | None = None
    allowed_model_tiers: list[Tier] | None = Field(default=None, min_length=1)
    user_daily_token_limit: int | None = Field(default=None, gt=0)
    org_monthly_token_limit: int | None = Field(default=None, gt=0)
    max_concurrent_tasks: int | None = Field(default=None, ge=1, le=1000)
    max_schedules: int | None = Field(default=None, ge=0, le=10000)
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    display_settings: dict[str, Any] | None = None
    # Explicit nulls lift a limit; omitted fields stay as they are.
    clear_user_daily_token_limit: bool = False
    clear_org_monthly_token_limit: bool = False

    @model_validator(mode="after")
    def _default_in_allowed(self) -> "SettingsUpdate":
        if self.allowed_model_tiers is not None:
            self.allowed_model_tiers = sorted(
                set(self.allowed_model_tiers), key=["fast", "standard", "advanced"].index
            )
        return self


SETTINGS_COLUMNS = (
    # app.model_tier[] is a domain array psycopg does not know: read it as text[].
    "default_model_tier::text, allowed_model_tiers::text[] as allowed_model_tiers, user_daily_token_limit, "
    "org_monthly_token_limit, "
    "max_concurrent_tasks, max_schedules, retention_days, display_settings, updated_at"
)


@router.get("/settings")
async def get_settings_(
    identity: ConsoleIdentity = Depends(require_admin), uow: UnitOfWork = Depends(get_uow)
) -> dict[str, Any]:
    row = await fetch_one(uow.connection, f"select {SETTINGS_COLUMNS} from app.org_settings")  # noqa: S608
    assert row is not None
    return row


@router.patch("/settings")
async def update_settings(
    body: SettingsUpdate,
    identity: ConsoleIdentity = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    conn = uow.connection
    current = await fetch_one(
        conn,
        "select default_model_tier::text, allowed_model_tiers::text[] as allowed_model_tiers "
        "from app.org_settings",
    )
    assert current is not None
    allowed = body.allowed_model_tiers or current["allowed_model_tiers"]
    default = body.default_model_tier or current["default_model_tier"]
    if default not in allowed:
        raise ApiError(422, "invalid", "The default model tier must be one of the allowed tiers.")
    changes: dict[str, Any] = dict(
        body.model_dump(
            exclude_none=True, exclude={"clear_user_daily_token_limit", "clear_org_monthly_token_limit"}
        ).items()
    )
    if body.clear_user_daily_token_limit:
        changes["user_daily_token_limit"] = None
    if body.clear_org_monthly_token_limit:
        changes["org_monthly_token_limit"] = None
    if not changes:
        raise ApiError(422, "invalid", "Nothing to change.")
    if "display_settings" in changes:
        changes["display_settings"] = Jsonb(changes["display_settings"])
    if "allowed_model_tiers" in changes or "default_model_tier" in changes:
        changes["allowed_model_tiers"], changes["default_model_tier"] = allowed, default
    assignments = ", ".join(f"{column} = %({column})s" for column in changes)
    if "allowed_model_tiers" in changes:
        assignments = assignments.replace(
            "%(allowed_model_tiers)s", "%(allowed_model_tiers)s::app.model_tier[]"
        )
    await conn.execute(f"update app.org_settings set {assignments}", changes)  # noqa: S608 - columns from the model
    audited = {k: (v if not isinstance(v, Jsonb) else "updated") for k, v in changes.items()}
    await audit(conn, identity, "settings.updated", None, audited)
    return await get_settings_(identity, uow)


# ------------------------------------------------------------------ audit and access requests


@router.get("/audit")
async def audit_log(
    identity: ConsoleIdentity = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
    limit: int = Query(50, ge=1, le=200),
    before: datetime | None = None,
    action: str | None = Query(None, max_length=100),
    decision: Literal["allowed", "denied", "info"] | None = None,
    actor_type: Literal["user", "agent", "system"] | None = None,
) -> dict[str, Any]:
    clauses, params = [], {"limit": limit + 1}
    if before is not None:
        clauses.append("a.occurred_at < %(before)s")
        params["before"] = before  # type: ignore[assignment]
    if action:
        clauses.append("a.action like %(action)s")
        params["action"] = action.replace("%", "").replace("_", r"\_") + "%"  # type: ignore[assignment]
    if decision:
        clauses.append("a.decision = %(decision)s")
        params["decision"] = decision  # type: ignore[assignment]
    if actor_type:
        clauses.append("a.actor_type = %(actor_type)s")
        params["actor_type"] = actor_type  # type: ignore[assignment]
    where = ("where " + " and ".join(clauses)) if clauses else ""
    rows = await fetch_all(
        uow.connection,
        f"""
        select a.id, a.occurred_at, a.actor_type, a.actor_id, u.email as actor_email, a.task_id,
               a.action, a.target, a.decision, a.reason, a.metadata
          from app.audit_log a
          left join app.org_memberships m on m.id = a.actor_id
          left join app.users u on u.id = m.user_id
          {where}
         order by a.occurred_at desc, a.id desc
         limit %(limit)s
        """,  # noqa: S608 - clauses are fixed strings
        params,
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": rows, "next_before": rows[-1]["occurred_at"].isoformat() if more and rows else None}


@router.get("/access-requests")
async def access_requests(
    identity: ConsoleIdentity = Depends(require_admin), uow: UnitOfWork = Depends(get_uow)
) -> list[dict[str, Any]]:
    return await fetch_all(
        uow.connection,
        "select id, external_user_id, email, status, created_at, resolved_at from app.access_requests "
        "order by (status = 'pending') desc, created_at desc limit 200",
    )

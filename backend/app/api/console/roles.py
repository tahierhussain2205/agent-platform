"""Console: custom roles. A role's profile shapes how the agent answers its holders (spec 4.2, D-202);
roles never grant access. A preview shows the difference a profile makes before it is saved."""

import secrets
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError

from app.agent.roles import Profile
from app.api.console.common import audit, fetch_all, fetch_one
from app.api.errors import ApiError
from app.api.temporal_client import temporal_client
from app.auth.console import ConsoleIdentity, console_identity, require_admin
from app.db.uow import UnitOfWork, get_uow
from app.settings import get_settings
from app.workflows.roles import SAMPLE_REQUESTS, PreviewRequest

router = APIRouter(prefix="/api/roles")

ROLES_SQL = """
select r.id, r.title, r.description, r.profile, r.status, r.created_at, r.updated_at,
       count(mr.membership_id) filter (where m.status = 'active') as members,
       count(mr.membership_id) filter (where m.status = 'active' and mr.is_primary) as primary_for
  from app.custom_roles r
  left join app.membership_roles mr on mr.role_id = r.id
  left join app.org_memberships m on m.id = mr.membership_id
 where {where}
 group by r.id
 order by r.status, lower(r.title)
"""


class RoleIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=4000)
    profile: Profile = Field(default_factory=Profile)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value.strip()


class RolePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=4000)
    profile: Profile | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("title must not be blank")
        return value.strip() if value else value


async def _role(conn: Any, role_id: UUID) -> dict[str, Any]:
    rows = await fetch_all(conn, ROLES_SQL.format(where="r.id = %s"), (role_id,))
    if not rows:
        raise ApiError(404, "not_found", "No such role.")
    return rows[0]


@router.get("")
async def list_roles(
    identity: ConsoleIdentity = Depends(console_identity),
    uow: UnitOfWork = Depends(get_uow),
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    return await fetch_all(
        uow.connection, ROLES_SQL.format(where="true" if include_archived else "r.status = 'active'")
    )


@router.post("", status_code=201)
async def create_role(
    body: RoleIn, identity: ConsoleIdentity = Depends(require_admin), uow: UnitOfWork = Depends(get_uow)
) -> dict[str, Any]:
    row = await fetch_one(
        uow.connection,
        "insert into app.custom_roles (org_id, title, description, profile) values (%s, %s, %s, %s) "
        "returning id",
        (identity.org_id, body.title, body.description, Jsonb(body.profile.model_dump(exclude_none=True))),
    )
    assert row is not None
    await audit(uow.connection, identity, "role.created", body.title)
    return await _role(uow.connection, row["id"])


@router.patch("/{role_id}")
async def update_role(
    role_id: UUID,
    body: RolePatch,
    identity: ConsoleIdentity = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise ApiError(422, "invalid", "Nothing to change.")
    current = await _role(uow.connection, role_id)
    if current["status"] != "active":
        raise ApiError(409, "archived", "Archived roles cannot be edited.")
    sets = ", ".join(f"{k} = %({k})s" for k in changes)
    values = {k: Jsonb(v) if k == "profile" else v for k, v in changes.items()}
    await uow.connection.execute(
        f"update app.custom_roles set {sets} where id = %(id)s",  # noqa: S608 - columns from the model
        {**values, "id": role_id},
    )
    await audit(
        uow.connection,
        identity,
        "role.updated",
        changes.get("title", current["title"]),
        {"fields": sorted(changes)},
    )
    return await _role(uow.connection, role_id)


@router.post("/{role_id}/archive")
async def archive_role(
    role_id: UUID, identity: ConsoleIdentity = Depends(require_admin), uow: UnitOfWork = Depends(get_uow)
) -> dict[str, Any]:
    current = await _role(uow.connection, role_id)
    if current["status"] == "archived":
        return current
    # Removing the assignments lets the database promote each holder's next oldest role to primary.
    await uow.connection.execute("delete from app.membership_roles where role_id = %s", (role_id,))
    await uow.connection.execute("update app.custom_roles set status = 'archived' where id = %s", (role_id,))
    await audit(uow.connection, identity, "role.archived", current["title"])
    return await _role(uow.connection, role_id)


# ------------------------------------------------------------------ preview


class PreviewIn(BaseModel):
    """The role as edited (saved or not) and one request: a sample, or the admin's own words."""

    title: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=4000)
    profile: Profile = Field(default_factory=Profile)
    sample: Literal["summary", "status", "fix"] | None = "summary"
    request: str | None = Field(default=None, max_length=300)


PREVIEW_PREFIX = "role-preview-"
UNAVAILABLE = "Previews are not available right now. Try again in a moment."


@router.post("/preview", status_code=202)
async def start_preview(
    body: PreviewIn,
    request: Request,
    identity: ConsoleIdentity = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    """Runs on the worker (the API holds no model key): the sample conversation answered without any
    role and as the role, metered to the org like any model call. Nothing is stored."""
    asked = (body.request or "").strip() or SAMPLE_REQUESTS[body.sample or "summary"]
    client = await temporal_client(request)
    if client is None:
        raise ApiError(503, "unavailable", UNAVAILABLE)
    preview_id = f"{PREVIEW_PREFIX}{identity.org_id}-{secrets.token_hex(8)}"
    try:
        await client.start_workflow(
            "RolePreviewWorkflow",
            PreviewRequest(
                str(identity.org_id),
                str(identity.membership_id),
                body.title.strip() or "This role",
                body.description,
                body.profile.model_dump(exclude_none=True),
                asked,
            ),
            id=preview_id,
            task_queue=get_settings().temporal_task_queue,
        )
    except Exception as exc:  # Temporal unreachable: nothing was started
        raise ApiError(503, "unavailable", UNAVAILABLE) from exc
    await audit(uow.connection, identity, "role.previewed", body.title.strip() or None)
    return {"id": preview_id, "request": asked}


@router.get("/preview/{preview_id}")
async def preview_result(
    preview_id: str, request: Request, identity: ConsoleIdentity = Depends(require_admin)
) -> dict[str, Any]:
    if not preview_id.startswith(f"{PREVIEW_PREFIX}{identity.org_id}-"):  # another org's preview
        raise ApiError(404, "not_found", "No such preview.")
    client = await temporal_client(request)
    if client is None:
        raise ApiError(503, "unavailable", UNAVAILABLE)
    handle = client.get_workflow_handle(preview_id)
    try:
        status = (await handle.describe()).status
    except (RPCError, RuntimeError) as exc:  # not found, or gone after its retention
        raise ApiError(404, "not_found", "No such preview.") from exc
    if status == WorkflowExecutionStatus.RUNNING:
        return {"status": "running"}
    if status != WorkflowExecutionStatus.COMPLETED:
        return {"status": "failed", "message": "The preview could not finish. Try again."}
    result = await handle.result()
    if result.get("stopped"):
        return {"status": "failed", "message": str(result["stopped"])}
    return {"status": "done", "without": result.get("without", ""), "with_role": result.get("with_role", "")}

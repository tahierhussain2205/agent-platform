"""Console: custom roles. Roles only shape how the agent responds (spec 4.2); they never grant access."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from app.api.console.common import audit, fetch_all, fetch_one
from app.api.errors import ApiError
from app.auth.console import ConsoleIdentity, console_identity, require_admin
from app.db.uow import UnitOfWork, get_uow

router = APIRouter(prefix="/api/roles")

ROLES_SQL = """
select r.id, r.title, r.description, r.status, r.created_at, r.updated_at,
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

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value.strip()


class RolePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=4000)

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
        "insert into app.custom_roles (org_id, title, description) values (%s, %s, %s) returning id",
        (identity.org_id, body.title, body.description),
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
    await uow.connection.execute(
        f"update app.custom_roles set {sets} where id = %(id)s",  # noqa: S608 - columns from the model
        {**changes, "id": role_id},
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

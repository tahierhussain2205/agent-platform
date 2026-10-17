"""Console: custom roles. A role's profile shapes how the agent answers its holders (spec 4.2, D-202);
its limits narrow what the agent does for them and route approvals to them (D-203, D-204). Roles never
grant access. A preview shows the difference a profile makes before it is saved."""

import secrets
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError

from app.agent.playbooks import BUILTIN, keys, latest
from app.agent.roles import Profile
from app.api.console.common import audit, fetch_all, fetch_one
from app.api.console.integrations import operations
from app.api.errors import ApiError
from app.api.temporal_client import temporal_client
from app.auth.console import ConsoleIdentity, console_identity, require_admin
from app.db.uow import UnitOfWork, get_uow
from app.settings import get_settings
from app.tools.base import Operation
from app.workflows.roles import SAMPLE_REQUESTS, PreviewRequest

router = APIRouter(prefix="/api/roles")

ROLES_SQL = """
select r.id, r.title, r.description, r.profile, r.allowed_playbooks, r.blocked_operations, r.ask_operations,
       r.approves_operations, r.max_mode::text as max_mode, r.status, r.created_at, r.updated_at,
       count(mr.membership_id) filter (where m.status = 'active') as members,
       count(mr.membership_id) filter (where m.status = 'active' and mr.is_primary) as primary_for
  from app.custom_roles r
  left join app.membership_roles mr on mr.role_id = r.id
  left join app.org_memberships m on m.id = mr.membership_id
 where {where}
 group by r.id
 order by r.status, lower(r.title)
"""


OperationList = list[str]
LIMIT_FIELDS = (
    "allowed_playbooks",
    "blocked_operations",
    "ask_operations",
    "approves_operations",
    "max_mode",
)


class Limits(BaseModel):
    """What the agent may do for the role's holders (D-203): only ever narrower than without the role.
    ``allowed_playbooks`` None means every playbook; ``max_mode`` "ask" keeps holders in Ask."""

    allowed_playbooks: list[str] | None = Field(default=None, max_length=50)
    blocked_operations: OperationList = Field(default_factory=list, max_length=200)
    ask_operations: OperationList = Field(default_factory=list, max_length=200)
    approves_operations: OperationList = Field(default_factory=list, max_length=200)
    max_mode: Literal["ask"] | None = None


class RoleIn(Limits):
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=4000)
    profile: Profile = Field(default_factory=Profile)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value.strip()


class RolePatch(Limits):
    """Only the fields sent change (a limit sent as null is cleared)."""

    title: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=4000)
    profile: Profile | None = None

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("title must not be blank")
        return value.strip() if value else value


def operation_catalog() -> dict[str, Operation]:
    """The operations a role can name: the native integrations' operations people can ask for
    (internal ones, such as moving a ticket when its change ships, follow from those)."""
    return {f"{key}.{op.name}": op for key in NATIVE for op in operations(key, {})}


def _check_limits(fields: dict[str, Any]) -> dict[str, Any]:
    """Known playbooks and operations only; only writes can ask or be approved. Sorted, without repeats."""
    catalog = operation_catalog()
    known_playbooks = set(keys(BUILTIN))
    out = dict(fields)
    if fields.get("allowed_playbooks") is not None:
        unknown = sorted(set(fields["allowed_playbooks"]) - known_playbooks)
        if unknown:
            raise ApiError(422, "invalid", f"Unknown playbook: {', '.join(unknown)}.")
        out["allowed_playbooks"] = sorted(set(fields["allowed_playbooks"]))
    for name in ("blocked_operations", "ask_operations", "approves_operations"):
        if name not in fields:
            continue
        values = sorted(set(fields[name] or []))
        unknown = [v for v in values if v not in catalog]
        if unknown:
            raise ApiError(422, "invalid", f"Unknown operation: {', '.join(unknown)}.")
        reads = [v for v in values if name != "blocked_operations" and catalog[v].tier == "read"]
        if reads:
            raise ApiError(422, "invalid", f"Only writes can ask or be approved: {', '.join(reads)}.")
        out[name] = values
    return out


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
    limits = _check_limits(body.model_dump(include=set(LIMIT_FIELDS)))
    row = await fetch_one(
        uow.connection,
        "insert into app.custom_roles (org_id, title, description, profile, allowed_playbooks, "
        "blocked_operations, ask_operations, approves_operations, max_mode) "
        "values (%(org)s, %(title)s, %(description)s, %(profile)s, %(allowed_playbooks)s, "
        "%(blocked_operations)s, %(ask_operations)s, %(approves_operations)s, %(max_mode)s) returning id",
        {
            "org": identity.org_id,
            "title": body.title,
            "description": body.description,
            "profile": Jsonb(body.profile.model_dump(exclude_none=True)),
            **limits,
        },
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
    changes = body.model_dump(exclude_unset=True)
    if any(changes.get(k, "") is None for k in ("title", "description", "profile")):
        raise ApiError(422, "invalid", "Title, description and profile cannot be null.")
    if not changes:
        raise ApiError(422, "invalid", "Nothing to change.")
    changes = _check_limits(changes)
    if "profile" in changes:
        changes["profile"] = Profile.model_validate(changes["profile"]).model_dump(exclude_none=True)
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


@router.get("/options")
async def role_options(identity: ConsoleIdentity = Depends(console_identity)) -> dict[str, Any]:
    """What a role's limits can name: playbooks, and operations grouped by integration."""
    return {
        "playbooks": [
            {"key": k, "title": latest(k).title, "description": latest(k).description} for k in keys(BUILTIN)
        ],
        "operations": [
            {
                "key": name,
                "integration": name.split(".", 1)[0],
                "operation": op.name,
                "tier": op.tier,
                "description": op.description,
            }
            for name, op in operation_catalog().items()
        ],
    }


# ------------------------------------------------------------------ preview


class PreviewIn(BaseModel):
    """The role as edited (saved or not) and one request: a sample, or the admin's own words."""

    title: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=4000)
    profile: Profile = Field(default_factory=Profile)
    sample: Literal["summary", "status", "fix"] | None = "summary"
    request: str | None = Field(default=None, max_length=300)


PREVIEW_PREFIX = "role-preview-"
NATIVE = ("slack", "jira", "github", "google_cloud_logging")
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

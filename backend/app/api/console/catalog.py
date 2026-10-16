"""Console: the service catalog (spec 3.2 step 3; plan phase 10). Members read it; admins propose
services from GitHub and Jira (a workflow within their own access), confirm, edit and archive them.
Only confirmed (active) services suggest owners to the agent. Every change is audited."""

import logging
import re
from datetime import timedelta
from typing import Any, Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import WorkflowAlreadyStartedError

from app.api.console.common import audit, fetch_all, fetch_one
from app.api.errors import ApiError
from app.api.temporal_client import temporal_client
from app.auth.console import ConsoleIdentity, console_identity, require_admin
from app.db.uow import UnitOfWork, get_uow
from app.settings import get_settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/catalog")
PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
MAX_OWNERS = 20
MAX_CHANNELS = 20


def proposal_id(org_id: UUID) -> str:
    return f"catalog-propose-{org_id}"


async def _services(
    conn: Any, where: str = "s.status <> 'archived'", params: Any = None
) -> list[dict[str, Any]]:
    query = f"""
        select s.id, s.name, s.description, s.status, s.jira_project, s.jira_component, s.paths, s.evidence,
               s.log_source,
               s.repository_id, r.full_name as repository, s.confirmed_at, cu.email as confirmed_by_email,
               s.created_at
          from app.services s
          left join app.repositories r on r.id = s.repository_id
          left join app.org_memberships cm on cm.id = s.confirmed_by
          left join app.users cu on cu.id = cm.user_id
         where {where}
         order by s.status = 'proposed' desc, lower(s.name)
    """  # noqa: S608 - the clause is a fixed string from this module
    rows = await fetch_all(conn, query, params)
    ids = [r["id"] for r in rows]
    owners = await fetch_all(
        conn,
        "select so.service_id, so.membership_id, u.email, u.name, so.source, so.reason, m.status "
        "from app.service_owners so join app.org_memberships m on m.id = so.membership_id "
        "join app.users u on u.id = m.user_id where so.service_id = any(%s) order by u.email",
        (ids,),
    )
    channels = await fetch_all(
        conn,
        "select sc.service_id, c.id, c.name from app.service_channels sc join app.channels c on c.id = "
        "sc.channel_id where sc.service_id = any(%s) order by c.name",
        (ids,),
    )
    return [
        {
            "id": str(r["id"]),
            "name": r["name"],
            "description": r["description"],
            "status": r["status"],
            "repository": {"id": str(r["repository_id"]), "full_name": r["repository"]}
            if r["repository_id"]
            else None,
            "jira_project": r["jira_project"],
            "jira_component": r["jira_component"],
            "paths": r["paths"],
            "evidence": r["evidence"],
            "log_source": r["log_source"] or None,
            "confirmed_at": r["confirmed_at"],
            "confirmed_by": r["confirmed_by_email"],
            "created_at": r["created_at"],
            "owners": [
                {
                    "membership_id": str(o["membership_id"]),
                    "email": o["email"],
                    "name": o["name"],
                    "source": o["source"],
                    "reason": o["reason"],
                    "active": o["status"] == "active",
                }
                for o in owners
                if o["service_id"] == r["id"]
            ],
            "channels": [
                {"id": str(c["id"]), "name": c["name"]} for c in channels if c["service_id"] == r["id"]
            ],
        }
        for r in rows
    ]


async def _one(conn: Any, service_id: UUID, live: bool = True) -> dict[str, Any]:
    found = await _services(
        conn, "s.id = %s" + (" and s.status <> 'archived'" if live else ""), (service_id,)
    )
    if not found:
        raise ApiError(404, "not_found", "No such service.")
    return found[0]


@router.get("")
async def list_services(
    identity: ConsoleIdentity = Depends(console_identity), uow: UnitOfWork = Depends(get_uow)
) -> dict[str, Any]:
    conn = uow.connection
    last = await fetch_one(
        conn,
        "select occurred_at, metadata from app.audit_log where action = 'catalog.proposed' "
        "order by occurred_at desc limit 1",
    )
    return {
        "services": await _services(conn),
        "can_manage": identity.is_admin,
        "last_proposal": {"at": last["occurred_at"], **last["metadata"]} if last else None,
    }


@router.get("/options")
async def options(
    identity: ConsoleIdentity = Depends(require_admin), uow: UnitOfWork = Depends(get_uow)
) -> dict[str, Any]:
    """What an admin can pick when editing: active members, channels and known repositories."""
    conn = uow.connection
    members = await fetch_all(
        conn,
        "select m.id, u.email, u.name from app.org_memberships m join app.users u on u.id = m.user_id "
        "where m.status = 'active' order by u.email",
    )
    channels = await fetch_all(conn, "select id, name from app.channels where not archived order by name")
    repositories = await fetch_all(conn, "select id, full_name from app.repositories order by full_name")
    return {
        "members": [{"membership_id": str(m["id"]), "email": m["email"], "name": m["name"]} for m in members],
        "channels": [{"id": str(c["id"]), "name": c["name"]} for c in channels],
        "repositories": [{"id": str(r["id"]), "full_name": r["full_name"]} for r in repositories],
    }


class LogSourceBody(BaseModel):
    """Where a service's logs are: for Google Cloud Logging, the project, the resource type and the
    resource label carrying the service's name (its value defaults to the service's name)."""

    provider: Literal["google_cloud_logging"] = "google_cloud_logging"
    project: str = Field(pattern=r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$")
    resource_type: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.]{0,63}$")
    label: str = Field(default="service_name", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    value: str | None = Field(default=None, min_length=1, max_length=200, pattern=r'^[^"\\]+$')


class ServiceBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    repository_id: UUID | None = None
    jira_project: str | None = Field(default=None, max_length=10)
    jira_component: str | None = Field(default=None, max_length=255)
    paths: list[str] | None = Field(default=None, max_length=50)
    owners: list[UUID] | None = Field(default=None, max_length=MAX_OWNERS)
    channels: list[UUID] | None = Field(default=None, max_length=MAX_CHANNELS)
    log_source: LogSourceBody | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("a name is required")
        return value.strip() if value else value

    @field_validator("jira_project")
    @classmethod
    def _project(cls, value: str | None) -> str | None:
        if value and not PROJECT_RE.match(value.strip().upper()):
            raise ValueError("a Jira project key like PAY")
        return value.strip().upper() if value else None

    @field_validator("paths")
    @classmethod
    def _paths(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [p.strip() for p in value if p.strip()]
        if any(len(p) > 300 or ".." in p.split("/") for p in cleaned):
            raise ValueError("paths are directories or files inside the repository")
        return list(dict.fromkeys(cleaned))


def _taken(exc: psycopg.errors.UniqueViolation) -> ApiError:
    return ApiError(409, "name_taken", "Another service in the catalog has this name.")


async def _set_links(conn: Any, identity: ConsoleIdentity, service_id: UUID, body: ServiceBody) -> None:
    """Owners and channels as sent: owners not listed are removed, new ones are the admin's choice
    (``manual``); owners kept keep their source and reason."""
    if body.owners is not None:
        wanted = list(dict.fromkeys(body.owners))
        await conn.execute(
            "delete from app.service_owners where service_id = %s and not (membership_id = any(%s))",
            (service_id, wanted),
        )
        for member in wanted:
            await conn.execute(
                "insert into app.service_owners (org_id, service_id, membership_id, source) "
                "select %s, %s, m.id, 'manual' from app.org_memberships m "
                "where m.id = %s and m.status = 'active' "
                "on conflict (service_id, membership_id) do nothing",
                (identity.org_id, service_id, member),
            )
    if body.channels is not None:
        wanted = list(dict.fromkeys(body.channels))
        await conn.execute(
            "delete from app.service_channels where service_id = %s and not (channel_id = any(%s))",
            (service_id, wanted),
        )
        for channel in wanted:
            await conn.execute(
                "insert into app.service_channels (org_id, service_id, channel_id) values (%s, %s, %s) "
                "on conflict do nothing",
                (identity.org_id, service_id, channel),
            )


@router.post("", status_code=201)
async def create(
    body: ServiceBody, identity: ConsoleIdentity = Depends(require_admin), uow: UnitOfWork = Depends(get_uow)
) -> dict[str, Any]:
    """A service an admin adds by hand is confirmed from the start."""
    if not body.name:
        raise ApiError(422, "invalid", "A name is required.")
    conn = uow.connection
    try:
        async with conn.transaction():
            row = await fetch_one(
                conn,
                "insert into app.services (org_id, name, description, repository_id, jira_project, "
                "jira_component, paths, log_source, status, confirmed_by, confirmed_at) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, now()) returning id",
                (
                    identity.org_id,
                    body.name,
                    body.description,
                    body.repository_id,
                    body.jira_project,
                    body.jira_component or None,
                    body.paths or [],
                    Jsonb(body.log_source.model_dump(exclude_none=True) if body.log_source else {}),
                    identity.membership_id,
                ),
            )
            assert row is not None
            await _set_links(conn, identity, row["id"], body)
    except psycopg.errors.UniqueViolation as exc:
        raise _taken(exc) from exc
    await audit(conn, identity, "catalog.created", body.name, {"service_id": str(row["id"])})
    return await _one(conn, row["id"])


@router.patch("/{service_id}")
async def update(
    service_id: UUID,
    body: ServiceBody,
    identity: ConsoleIdentity = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    conn = uow.connection
    before = await _one(conn, service_id)
    fields = body.model_dump(exclude_unset=True, exclude={"owners", "channels"})
    if "log_source" in fields:
        fields["log_source"] = Jsonb(body.log_source.model_dump(exclude_none=True) if body.log_source else {})
    if "jira_component" in fields:
        fields["jira_component"] = fields["jira_component"] or None
    if "paths" in fields:
        fields["paths"] = fields["paths"] or []
    if "name" in fields and not fields["name"]:
        raise ApiError(422, "invalid", "A name is required.")
    try:
        async with conn.transaction():
            if fields:
                assignments = ", ".join(f"{k} = %s" for k in fields)
                await conn.execute(
                    f"update app.services set {assignments} where id = %s",  # noqa: S608 - columns from the model
                    (*fields.values(), service_id),
                )
            await _set_links(conn, identity, service_id, body)
    except psycopg.errors.UniqueViolation as exc:
        raise _taken(exc) from exc
    changed = sorted(body.model_dump(exclude_unset=True))
    await audit(
        conn, identity, "catalog.updated", before["name"], {"service_id": str(service_id), "fields": changed}
    )
    return await _one(conn, service_id)


@router.post("/{service_id}/confirm")
async def confirm(
    service_id: UUID, identity: ConsoleIdentity = Depends(require_admin), uow: UnitOfWork = Depends(get_uow)
) -> dict[str, Any]:
    """A proposal becomes part of the catalog: its owners are suggested from now on."""
    conn = uow.connection
    service = await _one(conn, service_id)
    if service["status"] == "proposed":
        await conn.execute(
            "update app.services set status = 'active', confirmed_by = %s, confirmed_at = now() "
            "where id = %s",
            (identity.membership_id, service_id),
        )
        await audit(conn, identity, "catalog.confirmed", service["name"], {"service_id": str(service_id)})
    return await _one(conn, service_id)


@router.post("/{service_id}/archive")
async def archive(
    service_id: UUID, identity: ConsoleIdentity = Depends(require_admin), uow: UnitOfWork = Depends(get_uow)
) -> dict[str, Any]:
    """Out of the catalog; proposals will not suggest this name again."""
    conn = uow.connection
    service = await _one(conn, service_id)
    await conn.execute("update app.services set status = 'archived' where id = %s", (service_id,))
    await audit(conn, identity, "catalog.archived", service["name"], {"service_id": str(service_id)})
    return {"id": str(service_id), "status": "archived"}


@router.post("/propose", status_code=202)
async def propose(
    request: Request, identity: ConsoleIdentity = Depends(require_admin), uow: UnitOfWork = Depends(get_uow)
) -> dict[str, Any]:
    """Start proposing services from what this admin can read in GitHub and Jira (one run per org at a
    time). Proposals arrive as services with status ``proposed``."""
    client = await temporal_client(request)
    if client is None:
        raise ApiError(503, "unavailable", "Proposals can't start right now. Try again in a minute.")
    workflow_id = proposal_id(identity.org_id)
    try:
        await client.start_workflow(
            "CatalogProposalWorkflow",
            args=[str(identity.org_id), str(identity.membership_id)],
            id=workflow_id,
            task_queue=get_settings().temporal_task_queue,
            execution_timeout=timedelta(minutes=15),
        )
    except WorkflowAlreadyStartedError as exc:
        raise ApiError(409, "already_running", "Services are being proposed already.") from exc
    except Exception as exc:
        log.warning("could not start a catalog proposal", exc_info=True)
        raise ApiError(503, "unavailable", "Proposals can't start right now. Try again in a minute.") from exc
    await audit(uow.connection, identity, "catalog.propose_requested", "service catalog")
    return {"status": "running"}


@router.get("/proposal")
async def proposal(request: Request, identity: ConsoleIdentity = Depends(require_admin)) -> dict[str, Any]:
    """The latest proposal run: ``none``, ``running``, ``completed`` (with counts) or ``failed``."""
    client = await temporal_client(request)
    if client is None:
        return {"status": "unknown"}
    handle = client.get_workflow_handle(proposal_id(identity.org_id))
    try:
        described = await handle.describe()
    except Exception:  # never started (or retention passed)
        return {"status": "none"}
    if described.status == WorkflowExecutionStatus.RUNNING:
        return {"status": "running"}
    if described.status == WorkflowExecutionStatus.COMPLETED:
        result: dict[str, Any] = await handle.result()
        return {"status": "completed", "finished_at": described.close_time, **result}
    return {"status": "failed", "finished_at": described.close_time}

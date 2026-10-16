"""Console: repositories (plan phase 12). Members see each repository's protected paths and setup
recipe (how its tests run in a sandbox, and what happened last time); admins edit them. A change
touching a protected path needs a person's approval even in Auto; a test command set here is used
as is (an empty one goes back to detection). Every change is audited."""

import re
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator

from app.api.console.common import audit, fetch_all, fetch_one
from app.api.errors import ApiError
from app.auth.console import ConsoleIdentity, console_identity, require_admin
from app.db.uow import UnitOfWork, get_uow

router = APIRouter(prefix="/api/repositories")
PATH_RE = re.compile(r"^[A-Za-z0-9_.*?\[\]/@+-]{1,200}$")
MAX_PATHS = 50


class RepositoryBody(BaseModel):
    protected_paths: list[str] | None = Field(default=None, max_length=MAX_PATHS)
    test_command: str | None = Field(default=None, max_length=300)

    @field_validator("protected_paths")
    @classmethod
    def _paths(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        for raw in value:
            path = raw.strip().lstrip("/")
            if not path:
                continue
            if not PATH_RE.match(path) or ".." in path.split("/"):
                raise ValueError(f"{raw[:60]!r} is not a path or glob inside the repository")
            if path not in cleaned:
                cleaned.append(path)
        return cleaned

    @field_validator("test_command")
    @classmethod
    def _command(cls, value: str | None) -> str | None:
        if value is None:
            return None
        command = " ".join(value.split())
        if any(ch in value for ch in "\n\r\0"):
            raise ValueError("one line, no control characters")
        return command


def _out(row: dict[str, Any]) -> dict[str, Any]:
    recipe = row["setup_recipe"] or {}
    return {
        "id": str(row["id"]),
        "full_name": row["full_name"],
        "default_branch": row["default_branch"],
        "status": row["status"],
        "protected_paths": list(row["protected_paths"] or []),
        "recipe": {
            "test": recipe.get("test"),
            "source": recipe.get("source"),
            "status": recipe.get("status"),
            "last_run": recipe.get("last_run"),
        },
        "services": row.get("services") or [],
    }


QUERY = """
    select r.id, r.full_name, r.default_branch, r.status, r.protected_paths, r.setup_recipe,
           coalesce(array(select s.name from app.services s where s.repository_id = r.id
                          and s.status <> 'archived' order by s.name), '{}') as services
      from app.repositories r
"""


@router.get("")
async def list_repositories(
    identity: ConsoleIdentity = Depends(console_identity), uow: UnitOfWork = Depends(get_uow)
) -> dict[str, Any]:
    rows = await fetch_all(
        uow.connection, QUERY + " where r.status <> 'archived' order by lower(r.full_name)"
    )
    return {"repositories": [_out(r) for r in rows], "can_manage": identity.is_admin}


@router.patch("/{repository_id}")
async def update_repository(
    repository_id: UUID,
    body: RepositoryBody,
    identity: ConsoleIdentity = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    conn = uow.connection
    row = await fetch_one(conn, QUERY + " where r.id = %s", (repository_id,))
    if row is None:
        raise ApiError(404, "not_found", "No such repository.")
    changed = body.model_dump(exclude_unset=True)
    if "protected_paths" in changed:
        await conn.execute(
            "update app.repositories set protected_paths = %s where id = %s",
            (body.protected_paths or [], repository_id),
        )
    if "test_command" in changed:
        recipe = dict(row["setup_recipe"] or {})
        if body.test_command:
            recipe.update({"test": body.test_command, "source": "admin", "status": "untested"})
            recipe.pop("last_run", None)
        else:
            recipe = {}  # detected again on the next change
        await conn.execute(
            "update app.repositories set setup_recipe = %s where id = %s", (Jsonb(recipe), repository_id)
        )
    await audit(
        conn, identity, "repository.updated", row["full_name"],
        {"repository_id": str(repository_id), "fields": sorted(changed),
         "protected_paths": body.protected_paths if "protected_paths" in changed else None},
    )  # fmt: skip
    updated = await fetch_one(conn, QUERY + " where r.id = %s", (repository_id,))
    assert updated is not None
    return _out(updated)

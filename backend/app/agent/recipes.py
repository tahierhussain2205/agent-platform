"""Setup recipes (spec 8.1): how to run a repository's tests in its sandbox.

A recipe is detected from the repository's files the first time and saved on the repository
(``repositories.setup_recipe``) with what happened when it last ran; an admin can set the command
in the console. A recipe that worked is reused. One that could not run (the tool is missing in the
sandbox, nothing was collected) is reported as such, never as a pass.

``{"test": "...", "source": "detected" | "admin", "status": "untested" | "works" | "failed" |
"unavailable", "last_run": {"at", "exit_code", "summary", "task_id"}}``
"""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db.tenant import tenant_tx
from app.sandbox import Ran, SandboxError, SandboxProvider
from app.sandbox.workspaces import Workspace

PYTEST = "python -m pytest -q -p no:cacheprovider"
NOT_RUN = (126, 127)  # the command could not be found or run
PYTEST_NOTHING = 5  # pytest collected no tests


def detect(files: list[str], package_json: dict[str, Any] | None = None) -> str | None:
    """A test command from the repository's file list (paths relative to its root)."""
    names = set(files)
    python_tests = any(
        p.endswith(".py")
        and (p.startswith("tests/") or "/tests/" in p or p.rsplit("/", 1)[-1].startswith("test_"))
        for p in files
    )
    if python_tests:
        return PYTEST
    scripts = (package_json or {}).get("scripts") or {}
    if "package.json" in names and isinstance(scripts, dict) and scripts.get("test"):
        return "npm test --silent"
    if "go.mod" in names:
        return "go test ./..."
    if "Cargo.toml" in names:
        return "cargo test --quiet"
    return None


def outcome(ran: Ran) -> str:
    """works, failed (the tests ran and failed) or unavailable (they could not run)."""
    if ran.ok:
        return "works"
    text = (ran.stdout + ran.stderr).lower()
    if ran.exit_code in NOT_RUN or "command not found" in text or "no module named pytest" in text:
        return "unavailable"
    if ran.exit_code == PYTEST_NOTHING and "no tests ran" in text:
        return "unavailable"
    return "failed"


async def stored(pool: Any, org_id: UUID | str, repo: str) -> tuple[UUID | None, dict[str, Any], list[str]]:
    """(the repository's row id, its recipe, its protected paths); an unknown repository has none."""
    async with tenant_tx(pool, org_id) as conn:
        row = await (
            await conn.execute(
                "select id, setup_recipe, protected_paths from app.repositories "
                "where full_name = %s and status = 'active' order by created_at limit 1",
                (repo,),
            )
        ).fetchone()
    if row is None:
        return None, {}, []
    return row[0], dict(row[1] or {}), list(row[2] or [])


async def ensure(pool: Any, org_id: UUID | str, provider: SandboxProvider, ws: Workspace) -> str | None:
    """The repository's test command: the saved one, or one detected now (and saved, untested)."""
    repository_id, recipe, _ = await stored(pool, org_id, ws.repository)
    if recipe.get("test") and (recipe.get("source") == "admin" or recipe.get("status") != "unavailable"):
        return str(recipe["test"])
    try:
        entries = await provider.list_files(ws.sandbox_id, ws.root, recursive=True)
    except SandboxError:
        return None
    files = [str(e["path"]).removeprefix(ws.root + "/") for e in entries if e.get("type") == "file"]
    package = None
    if "package.json" in files:
        try:
            package = json.loads(await provider.read_bytes(ws.sandbox_id, ws.path("package.json")))
        except (SandboxError, ValueError):
            package = None
    command = detect(files, package)
    if command and repository_id is not None and command != recipe.get("test"):
        await save(pool, org_id, repository_id, {"test": command, "source": "detected", "status": "untested"})
    return command


async def record(pool: Any, org_id: UUID | str, repo: str, command: str, ran: Ran, task_id: str) -> str:
    """Save what happened when the recipe ran; returns its status."""
    status = outcome(ran)
    repository_id, recipe, _ = await stored(pool, org_id, repo)
    if repository_id is not None and recipe.get("test", command) == command:
        tail = (ran.stdout + ran.stderr).strip().splitlines()[-1:] or [""]
        await save(pool, org_id, repository_id, {
            **recipe, "test": command, "source": recipe.get("source") or "detected", "status": status,
            "last_run": {"at": datetime.now(UTC).isoformat(), "exit_code": ran.exit_code,
                         "summary": tail[0][:200], "task_id": task_id},
        })  # fmt: skip
    return status


async def save(pool: Any, org_id: UUID | str, repository_id: UUID, recipe: dict[str, Any]) -> None:
    async with tenant_tx(pool, org_id) as conn:
        await conn.execute(
            "update app.repositories set setup_recipe = %s where id = %s", (Jsonb(recipe), repository_id)
        )

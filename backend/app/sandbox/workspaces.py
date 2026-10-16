"""A task's sandboxes: one per repository, at one base commit, shared by the task's investigators.

The first caller claims the ``sandboxes`` row (``insert ... on conflict``) and builds the sandbox:
it pins the base commit, has the platform fetch the repository archive at that commit (with the
caller's read only credential, outside the sandbox) and uploads it. Others wait until the row is
ready. A failed build can be retried by the next caller. The task's end destroys its sandboxes; the
maintenance job destroys what expired.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from app.db.tenant import tenant_tx
from app.sandbox import Limits, SandboxError, SandboxProvider

log = logging.getLogger(__name__)
WAIT_SECONDS = 120.0


@dataclass(frozen=True)
class Workspace:
    """A ready sandbox: where the repository is in it, and at which commit."""

    sandbox_id: str
    repository: str
    base_sha: str
    root: str  # the repository's directory inside the sandbox

    def path(self, relative: str) -> str:
        return f"{self.root}/{relative.lstrip('/')}" if relative.strip("/") else self.root


def root_of(repository: str) -> str:
    return repository.replace("/", "__")


class Workspaces:
    def __init__(self, pool: Any, provider: SandboxProvider, limits: Limits | None = None) -> None:
        self.pool = pool
        self.provider = provider
        self.limits = limits or Limits()

    async def ensure(
        self,
        org_id: UUID,
        task_id: UUID,
        repository: str,
        head: Callable[[], Awaitable[str]],
        archive: Callable[[str], Awaitable[bytes]],
    ) -> Workspace:
        """The task's sandbox for a repository, built on first use."""
        deadline = time.monotonic() + WAIT_SECONDS
        while True:
            claimed = await self._claim(org_id, task_id, repository)
            if claimed:
                return await self._build(org_id, claimed, repository, head, archive)
            row = await self._row(org_id, task_id, repository)
            if row is None:
                continue  # removed meanwhile: claim again
            status, provider_id, base_sha, _error = row
            if status == "ready":
                return Workspace(provider_id, repository, base_sha, root_of(repository))
            if status == "destroyed":
                raise SandboxError("the task's sandbox was destroyed; the task has ended")
            if status == "failed":
                again = await self._retry(org_id, task_id, repository)
                if again:
                    return await self._build(org_id, again, repository, head, archive)
                continue  # someone else retries it
            if time.monotonic() > deadline:
                raise SandboxError("the sandbox is taking too long to prepare")
            await asyncio.sleep(0.5)

    async def _claim(self, org_id: UUID, task_id: UUID, repository: str) -> UUID | None:
        async with tenant_tx(self.pool, org_id) as conn:
            row = await (
                await conn.execute(
                    "insert into app.sandboxes (org_id, task_id, repository, expires_at) "
                    "values (%s, %s, %s, now() + %s) on conflict (task_id, repository) do nothing "
                    "returning id",
                    (org_id, task_id, repository, timedelta(seconds=self.limits.ttl_seconds)),
                )
            ).fetchone()
        return row[0] if row else None

    async def _retry(self, org_id: UUID, task_id: UUID, repository: str) -> UUID | None:
        """Take a failed build back to ``creating``: this caller builds it again."""
        async with tenant_tx(self.pool, org_id) as conn:
            row = await (
                await conn.execute(
                    "update app.sandboxes set status = 'creating', error = null, provider_id = null, "
                    "base_sha = null, expires_at = now() + %s "
                    "where task_id = %s and repository = %s and status = 'failed' returning id",
                    (timedelta(seconds=self.limits.ttl_seconds), task_id, repository),
                )
            ).fetchone()
        return row[0] if row else None

    async def _row(
        self, org_id: UUID, task_id: UUID, repository: str
    ) -> tuple[str, str, str, str | None] | None:
        async with tenant_tx(self.pool, org_id) as conn:
            row = await (
                await conn.execute(
                    "select status, provider_id, base_sha, error from app.sandboxes "
                    "where task_id = %s and repository = %s",
                    (task_id, repository),
                )
            ).fetchone()
        return (row[0], row[1], row[2], row[3]) if row else None

    async def _build(
        self,
        org_id: UUID,
        sandbox_row: UUID,
        repository: str,
        head: Callable[[], Awaitable[str]],
        archive: Callable[[str], Awaitable[bytes]],
    ) -> Workspace:
        started = time.monotonic()
        provider_id: str | None = None
        try:
            base_sha = await head()
            provider_id = await self.provider.create(
                self.limits, {"repository": repository, "org": str(org_id)}
            )
            await self._record(org_id, sandbox_row, provider_id=provider_id, base_sha=base_sha)
            files = await self.provider.upload(
                provider_id, await archive(base_sha), root_of(repository), strip=1
            )
        except Exception as exc:
            reason = (
                str(exc)
                if isinstance(exc, SandboxError)
                else f"the repository could not be prepared ({type(exc).__name__})"
            )
            await self._record(org_id, sandbox_row, status="failed", error=reason[:500])
            if provider_id:
                try:
                    await self.provider.destroy(provider_id)
                except SandboxError:
                    log.warning("could not destroy a failed sandbox", extra={"repository": repository})
            raise SandboxError(reason) from exc
        await self._record(org_id, sandbox_row, status="ready")
        log.info("sandbox ready", extra={"repository": repository, "files": files,
                                         "seconds": round(time.monotonic() - started, 2)})  # fmt: skip
        return Workspace(provider_id, repository, base_sha, root_of(repository))

    async def _record(self, org_id: UUID, sandbox_row: UUID, **fields: Any) -> None:
        assignments = ", ".join(f"{k} = %s" for k in fields)
        async with tenant_tx(self.pool, org_id) as conn:
            await conn.execute(
                f"update app.sandboxes set {assignments} where id = %s",  # noqa: S608 - fixed column names
                (*fields.values(), sandbox_row),
            )

    async def release_task(self, org_id: UUID | str, task_id: UUID | str) -> int:
        """Destroy every sandbox of a task (its end). Returns how many were destroyed."""
        async with tenant_tx(self.pool, org_id) as conn:
            rows = await (
                await conn.execute(
                    "select id, provider_id from app.sandboxes where task_id = %s and status <> 'destroyed'",
                    (task_id,),
                )
            ).fetchall()
        for sandbox_row, provider_id in rows:
            if provider_id:
                try:
                    await self.provider.destroy(provider_id)
                except SandboxError:
                    log.warning("could not destroy a sandbox; it expires on its own", exc_info=True)
                    continue
            async with tenant_tx(self.pool, org_id) as conn:
                await conn.execute(
                    "update app.sandboxes set status = 'destroyed', destroyed_at = now() where id = %s",
                    (sandbox_row,),
                )
                # Metered: how long the task held the sandbox (usage, like tokens).
                await conn.execute(
                    "insert into app.usage_events (org_id, membership_id, task_id, kind, sandbox_seconds) "
                    "select s.org_id, t.requested_by, s.task_id, 'sandbox', "
                    "extract(epoch from s.destroyed_at - s.created_at) "
                    "from app.sandboxes s join app.tasks t on t.id = s.task_id where s.id = %s",
                    (sandbox_row,),
                )
        return len(rows)

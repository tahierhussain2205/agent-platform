"""Code and recent changes for investigations: code is read in the task's sandbox at a pinned
commit (one sandbox per task and repository, built once even when investigators run in parallel),
the installation token never enters it, secret files are never read, the requester's access is
checked first, and sandboxes go when the task ends or expires."""

import asyncio
import dataclasses
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg
import pytest
import pytest_asyncio

from app.maintenance import sandbox_cleanup_job
from app.sandbox import HttpSandboxProvider
from app.sandbox.workspaces import Workspaces
from app.tools.executor import ToolExecutor
from app.tools.github_code import secret_path
from tests.connect import connect_github, enable, link_github, sql
from tests.stack import FAKES, PROVIDER_SETTINGS, Stack, fakes_up
from tests.tools.test_integrations import Setup

pytestmark = pytest.mark.skipif(not fakes_up(), reason="stand-ins not reachable (run `make infra`)")
REPO = "acme/cart-service"


@pytest_asyncio.fixture(loop_scope="session")
async def setup(stack: Stack) -> AsyncIterator[Setup]:
    acme = stack.env.world.acme
    enable(stack, acme, "github")
    await connect_github(stack, acme)
    await link_github(stack, acme, acme.user, "bob-gh")
    async with httpx.AsyncClient(timeout=30) as http:
        yield Setup(stack, http)
    sql(stack, "update app.tasks set status = 'completed', finished_at = now() where status = 'running'")


async def admin(path: str, method: str = "GET", **kw: Any) -> Any:
    async with httpx.AsyncClient() as client:
        return (await client.request(method, f"{FAKES}/_admin/sandbox{path}", **kw)).json()


def sandboxes(setup: Setup, task: Any) -> list[tuple[str, str, str | None, str | None]]:
    rows: list[tuple[str, str, str | None, str | None]] = sql(
        setup.stack,
        "select repository, status, base_sha, provider_id from app.sandboxes where task_id = %s",
        (task,),
    )
    return rows


def ago(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_secret_files_are_recognised() -> None:
    for path in (".env", "deploy/.env", ".env.production", "certs/server.pem", "id_rsa", "infra/prod.tfstate",
                 "config/credentials.json", "gcp-service-account.json", ".npmrc"):  # fmt: skip
        assert secret_path(path), path
    for path in (".env.example", "config/.env.sample", "src/pricing/cart.py", "README.md", "keys.py"):
        assert not secret_path(path), path


async def test_code_is_searched_and_read_at_a_pinned_commit(setup: Setup) -> None:
    bob = await setup.executor("bob")
    found = await bob.run("github_search_code", {"repo": REPO, "pattern": "prices[n]"})
    assert not found.is_error, found.text
    (row,) = sandboxes(setup, bob.ctx.task_id)
    assert row[:2] == (REPO, "ready")
    base = str(row[2])
    assert found.data == {"repo": REPO, "base_sha": base, "files": ["src/pricing/cart.py"]}
    assert f"{REPO}@{base[:7]}" in found.text
    assert "L7: prices[n] = item.price" in found.text
    (source,) = list(bob.ctx.refs.by_ref.values())
    assert source.ref == f"github:{REPO}@{base[:7]}:src/pricing/cart.py#L7-L7"
    assert source.permalink == f"https://github.com/{REPO}/blob/{base}/src/pricing/cart.py#L7-L7"
    assert (source.visibility, source.audience) == ("restricted", f"github:{REPO}")
    read = await bob.run(
        "github_read_code", {"repo": REPO, "path": "src/pricing/cart.py", "start": 1, "end": 8}
    )
    assert "1: MAX_LINES = 19  # BUG: assumes at most 19 items" in read.text
    assert "lines 1-8 of 8" in read.text
    listing = await bob.run("github_list_files", {"repo": REPO, "recursive": True})
    assert "src/pricing/cart.py" in listing.data["files"]
    assert "deploy/.env" not in listing.data["files"]  # secret files are not even listed


async def test_one_sandbox_for_parallel_investigators_and_no_token_inside(setup: Setup) -> None:
    bob = await setup.executor("bob")
    # Two investigators of one task at once (each with its own context, as the workflow runs them).
    other = ToolExecutor(dataclasses.replace(bob.ctx, cache={}), bob.registry, bob.policy)
    first, second = await asyncio.gather(
        bob.run("github_search_code", {"repo": REPO, "pattern": "def total"}),
        other.run("github_read_code", {"repo": REPO, "path": "src/webhooks/verify.py"}),
    )
    assert not first.is_error, first.text
    assert not second.is_error, second.text
    (row,) = sandboxes(setup, bob.ctx.task_id)
    provider_id = str(row[3])
    live = [s["id"] for s in (await admin("/sandboxes"))["live"]]
    assert live.count(provider_id) == 1
    credential = await bob.credential_for("github", "org")
    assert credential is not None
    assert (await admin(f"/sandboxes/{provider_id}/grep", "POST", json={"text": credential.token}))[
        "hits"
    ] == []


async def test_secret_files_are_never_read_or_searched(setup: Setup) -> None:
    bob = await setup.executor("bob")
    refused = await bob.run("github_read_code", {"repo": REPO, "path": "deploy/.env"})
    assert refused.is_error
    assert "never read" in refused.text
    found = await bob.run("github_search_code", {"repo": REPO, "pattern": "WEBHOOK_SIGNING_KEY"})
    assert "whsec_committed_by_mistake" not in found.text
    assert "deploy/.env" not in found.data["files"]
    assert ".env.example" in found.data["files"]  # an example file is fine
    escape = await bob.run("github_read_code", {"repo": REPO, "path": "../../etc/passwd"})
    assert escape.is_error


async def test_the_requesters_access_is_checked_before_any_sandbox(setup: Setup) -> None:
    bob = await setup.executor("bob")
    denied = await bob.run("github_search_code", {"repo": "acme/infra", "pattern": "aws"})
    assert denied.is_error
    assert "can't read acme/infra" in denied.text
    assert sandboxes(setup, bob.ctx.task_id) == []


async def test_without_a_sandbox_provider_code_is_not_available(setup: Setup) -> None:
    bob = await setup.executor("bob")
    bob.ctx.settings = PROVIDER_SETTINGS.model_copy(update={"sandbox_url": None})
    missing = await bob.run("github_search_code", {"repo": REPO, "pattern": "total"})
    assert missing.is_error
    assert "no sandbox provider is configured" in missing.text
    single = await bob.run("github_read_file", {"repo": REPO, "path": "src/pricing/cart.py"})
    assert not single.is_error  # single files still come from the API


async def test_recent_changes_and_deployments(setup: Setup) -> None:
    bob = await setup.executor("bob")
    recent = await bob.run("github_list_commits", {"repo": REPO, "since": ago(12)})
    (commit,) = recent.data["commits"]
    assert (commit["message"], commit["files"]) == (
        "Preallocate price slots for faster totals",
        ["src/pricing/cart.py"],
    )
    assert "src/pricing/cart.py +" in recent.text
    by_path = await bob.run("github_list_commits", {"repo": REPO, "path": "src/webhooks"})
    assert [c["message"] for c in by_path.data["commits"]] == [
        "Reload the webhook signing key on rotation", "Verify webhook signatures with the signing key",
    ]  # fmt: skip
    detail = await bob.run("github_get_commit", {"repo": REPO, "sha": commit["sha"][:7]})
    assert "+    prices = [0.0] * MAX_LINES" in detail.text
    deploys = await bob.run("github_list_deployments", {"repo": REPO, "environment": "production"})
    assert deploys.data["deployments"][0]["sha"] == commit["sha"]
    assert "production:" in deploys.text


async def test_sandboxes_go_when_the_task_ends_or_expires(setup: Setup) -> None:
    bob = await setup.executor("bob")
    await bob.run("github_search_code", {"repo": REPO, "pattern": "total"})
    (row,) = sandboxes(setup, bob.ctx.task_id)
    provider = HttpSandboxProvider(f"{FAKES}/sandbox/v1", "sbx-local-key", setup.http)
    assert await Workspaces(setup.stack.pool, provider).release_task(setup.acme.id, bob.ctx.task_id) == 1
    assert sandboxes(setup, bob.ctx.task_id)[0][1] == "destroyed"
    assert row[3] not in [s["id"] for s in (await admin("/sandboxes"))["live"]]
    metered = sql(setup.stack, "select kind, sandbox_seconds > 0, membership_id from app.usage_events "
                               "where task_id = %s and kind = 'sandbox'", (bob.ctx.task_id,))  # fmt: skip
    assert metered == [("sandbox", True, setup.acme.user)]
    # Expired: the maintenance job destroys it at the provider and marks it.
    later = await setup.executor("bob")
    await later.run("github_search_code", {"repo": REPO, "pattern": "total"})
    (expired,) = sandboxes(setup, later.ctx.task_id)
    sql(setup.stack, "update app.sandboxes set expires_at = now() - interval '1 minute' where task_id = %s",
        (later.ctx.task_id,))  # fmt: skip
    _, job = sandbox_cleanup_job(PROVIDER_SETTINGS)

    def run() -> str:  # a cron job: synchronous, with its own event loop
        with psycopg.connect(setup.stack.env.world.url, autocommit=True) as conn:
            conn.execute("set role app_maintenance")
            return job(conn)

    assert (await asyncio.to_thread(run)).endswith("expired sandbox(es) destroyed")
    assert sandboxes(setup, later.ctx.task_id)[0][1] == "destroyed"
    assert expired[3] not in [s["id"] for s in (await admin("/sandboxes"))["live"]]

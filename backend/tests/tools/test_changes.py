"""Changing code (plan phase 12): the task's working copy (a sandbox with a baseline commit) is
written, edited and tested through the sandbox tools; its change is exported; and the trusted
pusher pushes it through the GitHub App: blobs, tree and commit on the base, a branch under agent/
(never the default branch, never forced), the pull request found or opened, idempotent by the
action's key, the requester's write access checked, the blocking checks run again."""

import base64
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio

from app.sandbox import HttpSandboxProvider
from app.sandbox.workspaces import Workspaces
from app.tools.executor import ToolExecutor
from tests.connect import connect_github, enable, link_github, sql
from tests.stack import FAKES, Stack, fakes_up
from tests.tools.test_integrations import Setup

pytestmark = pytest.mark.skipif(not fakes_up(), reason="stand-ins not reachable (run `make infra`)")
REPO = "acme/cart-service"
TEST = "tests/test_many.py"
TEST_BODY = (
    "from dataclasses import dataclass\n\nfrom src.pricing.cart import total\n\n\n@dataclass\nclass Item:\n"
    "    price: float\n\n\ndef test_many():\n    assert total([Item(2.0)] * 21) == 42.0\n"
)
OLD = "    prices = [0.0] * MAX_LINES  # preallocated for speed\n"
PYTEST = "python -m pytest -q -p no:cacheprovider"


@pytest_asyncio.fixture(loop_scope="session")
async def setup(stack: Stack) -> AsyncIterator[Setup]:
    acme = stack.env.world.acme
    enable(stack, acme, "github")
    await connect_github(stack, acme)
    await link_github(stack, acme, acme.user, "bob-gh")  # write access to cart-service
    await link_github(stack, acme, acme.admin, "quinn-gh")  # read access only
    async with httpx.AsyncClient(timeout=60) as http:
        yield Setup(stack, http)
    sql(stack, "update app.tasks set status = 'completed', finished_at = now() where status = 'running'")


async def github_state(repo: str = REPO) -> dict[str, Any]:
    async with httpx.AsyncClient() as client:
        state: dict[str, Any] = (await client.get(f"{FAKES}/_admin/github/repos/{repo}")).json()
    return state


async def prepared(setup: Setup, who: str = "bob", base: str | None = None) -> ToolExecutor:
    executor = await setup.executor(who)
    args: dict[str, Any] = {"repo": REPO, "push": True}
    if base:
        args["base_sha"] = base
    result = await executor.run("github_prepare_workspace", args)
    assert not result.is_error, result.text
    return executor


async def fixed(executor: ToolExecutor) -> None:
    """The reproduction test, then the fix (the stand-in's cart bug)."""
    for args in ({"repo": REPO, "path": TEST, "content": TEST_BODY},):
        assert not (await executor.run("sandbox_write_file", args)).is_error
    edit = await executor.run("sandbox_edit_file", {
        "repo": REPO, "path": "src/pricing/cart.py",
        "old": OLD + "    for n, item in enumerate(items):\n        prices[n] = item.price\n    return sum(prices)\n",
        "new": "    return sum(item.price for item in items)\n"})  # fmt: skip
    assert not edit.is_error, edit.text


def store_change(setup: Setup, executor: ToolExecutor, data: dict[str, Any]) -> str:
    rows = sql(
        setup.stack,
        "insert into app.artifacts (org_id, task_id, kind, content) values (%s, %s, 'diff', %s) returning id",
        (setup.acme.id, executor.ctx.task_id, json.dumps(data)),
    )
    return str(rows[0][0])


async def exported(setup: Setup, executor: ToolExecutor) -> tuple[str, dict[str, Any]]:
    result = await executor.run("sandbox_export_change", {"repo": REPO})
    assert not result.is_error, result.text
    return store_change(setup, executor, result.data), result.data


def push_args(change: str, base: str, branch: str, **extra: Any) -> dict[str, Any]:
    return {"repo": REPO, "change": change, "base_sha": base, "branch": branch, "title": "PAY-101: fix carts",
            "body": "The plan", "summary": "Sum every item", **extra}  # fmt: skip


async def test_the_working_copy_is_written_tested_and_exported(setup: Setup) -> None:
    bob = await prepared(setup)
    (row,) = sql(setup.stack, "select status, base_sha, provider_id from app.sandboxes where task_id = %s",
                 (bob.ctx.task_id,))  # fmt: skip
    assert row[0] == "ready"
    main = (await github_state())["refs"]["main"]
    assert row[1] == main  # a new change starts at the default branch's head
    before = await bob.run("sandbox_run_command", {"repo": REPO, "command": PYTEST})
    assert before.data["exit_code"] == 0, before.text  # the fixture's own test passes
    assert (
        await bob.run("sandbox_write_file", {"repo": REPO, "path": TEST, "content": TEST_BODY})
    ).text.startswith("Wrote tests/test_many.py")
    failing = await bob.run("sandbox_run_command", {"repo": REPO, "command": PYTEST})
    assert failing.data["exit_code"] == 1
    assert "IndexError" in failing.text  # reproduced
    ambiguous = await bob.run("sandbox_edit_file", {"repo": REPO, "path": "src/pricing/cart.py",
                                                    "old": "item", "new": "x"})  # fmt: skip
    assert ambiguous.is_error
    assert "appears" in ambiguous.text
    missing = await bob.run(
        "sandbox_edit_file", {"repo": REPO, "path": "src/pricing/cart.py", "old": "nope", "new": ""}
    )
    assert "does not appear" in missing.text
    await fixed(bob)
    passing = await bob.run("sandbox_run_command", {"repo": REPO, "command": PYTEST})
    assert passing.data["exit_code"] == 0, passing.text
    diff = await bob.run("sandbox_show_diff", {"repo": REPO})
    assert "+    return sum(item.price for item in items)" in diff.text
    _, data = await exported(setup, bob)
    assert data["base_sha"] == main
    by_path = {f["path"]: f for f in data["files"]}
    assert {p: f["status"] for p, f in by_path.items()} == {"src/pricing/cart.py": "M", TEST: "A"}
    assert (
        base64.b64decode(by_path[TEST]["content_b64"]).decode() == TEST_BODY
    )  # caches are not in the change


async def test_the_sandbox_tools_refuse_secrets_workflows_and_escapes(setup: Setup) -> None:
    bob = await prepared(setup)
    for path, reason in ((".github/workflows/ci.yml", "Workflow files"), ("deploy/.env", "secrets file"),
                         (".git/config", ".git directory"), ("../../etc/passwd", "file path inside")):  # fmt: skip
        refused = await bob.run("sandbox_write_file", {"repo": REPO, "path": path, "content": "x"})
        assert refused.is_error, path
        assert reason in refused.text, refused.text
    other = await setup.executor("bob")  # a task without a working copy
    none = await other.run("sandbox_run_command", {"repo": REPO, "command": "ls"})
    assert none.is_error
    assert "has no working copy" in none.text


async def test_preparing_a_change_needs_push_access(setup: Setup) -> None:
    alice = await setup.executor("alice")  # quinn-gh: read only
    refused = await alice.run("github_prepare_workspace", {"repo": REPO, "push": True})
    assert refused.is_error
    assert "can't push to acme/cart-service" in refused.text
    assert (
        sql(setup.stack, "select count(*) from app.sandboxes where task_id = %s", (alice.ctx.task_id,))[0][0]
        == 0
    )


async def test_the_pusher_pushes_once_to_a_new_branch_and_opens_the_pull_request(setup: Setup) -> None:
    bob = await prepared(setup)
    await fixed(bob)
    change, data = await exported(setup, bob)
    base = data["base_sha"]
    branch = f"agent/pay-101-fix-{str(bob.ctx.task_id)[-6:]}"
    key = f"task:{bob.ctx.task_id}:action:1"
    pushed = await bob.run("github_push_changes", {**push_args(change, base, branch), "_idempotency_key": key},
                           approved=True)  # fmt: skip
    assert not pushed.is_error, pushed.text
    sha, number = pushed.data["sha"], pushed.data["number"]
    assert pushed.data["branch"] == branch
    assert pushed.data["target_ref"] == f"github:{REPO}#{number}"
    state = await github_state()
    assert state["refs"][branch] == sha
    assert state["refs"]["main"] == base  # the default branch never moves
    (push,) = [p for p in state["pushes"] if p["ref"] == f"refs/heads/{branch}"]
    assert (push["before"], push["forced"], push["by"]) == (None, False, "agent-platform-local[bot]")
    commit = next(c for c in state["commits"] if c["sha"] == sha)
    assert commit["parents"] == [base]
    assert f"Agent-Action: {key}" in commit["message"].splitlines()
    assert sorted(f["filename"] for f in commit["files"]) == ["src/pricing/cart.py", TEST]
    (pr,) = [p for p in state["pulls"] if p["number"] == number]
    assert (pr["head"]["ref"], pr["base"]["ref"], pr["title"], pr["body"]) == (branch, "main", "PAY-101: fix carts",
                                                                               "The plan")  # fmt: skip
    # A retry (the answer was lost) finds its own commit and the pull request: nothing is pushed twice.
    again = await bob.run("github_push_changes", {**push_args(change, base, branch), "_idempotency_key": key},
                          approved=True)  # fmt: skip
    assert (again.data["sha"], again.data["number"]) == (sha, number)
    after = await github_state()
    assert len([p for p in after["pushes"] if p["ref"] == f"refs/heads/{branch}"]) == 1
    assert len([p for p in after["pulls"] if p["head"]["ref"] == branch]) == 1
    # Another action on the same branch prepared on the old base: the branch moved, nothing is forced.
    moved = await bob.run("github_push_changes", {**push_args(change, base, branch), "_idempotency_key": key + "x"},
                          approved=True)  # fmt: skip
    assert moved.is_error
    assert "moved" in moved.text
    assert (await github_state())["refs"][branch] == sha


async def test_a_revision_fast_forwards_the_pull_requests_branch(setup: Setup) -> None:
    bob = await prepared(setup)
    await fixed(bob)
    change, data = await exported(setup, bob)
    branch = f"agent/revise-{str(bob.ctx.task_id)[-6:]}"
    first = await bob.run("github_push_changes", {**push_args(change, data["base_sha"], branch),
                                                  "_idempotency_key": f"k1-{bob.ctx.task_id}"}, approved=True)  # fmt: skip
    head, number = first.data["sha"], first.data["number"]
    # A later task prepares the revision on the pull request's head.
    rev = await prepared(setup, base=head)
    pr = await rev.run("github_get_pull_request", {"repo": REPO, "number": number})
    assert (pr.data["head_ref"], pr.data["head_sha"], pr.data["state"]) == (branch, head, "open")
    edit = await rev.run("sandbox_edit_file", {"repo": REPO, "path": "src/pricing/cart.py", "old": "def total(items):\n",
                                               "new": 'def total(items):\n    """The sum of the prices."""\n'})  # fmt: skip
    assert not edit.is_error, edit.text
    change2, data2 = await exported(setup, rev)
    assert [f["path"] for f in data2["files"]] == [
        "src/pricing/cart.py"
    ]  # only the revision, against its base
    second = await rev.run("github_push_changes", {
        **push_args(change2, head, branch, pull_number=number, title="Add a docstring"),
        "_idempotency_key": f"k2-{rev.ctx.task_id}"}, approved=True)  # fmt: skip
    assert not second.is_error, second.text
    assert second.data["number"] == number
    state = await github_state()
    assert state["refs"][branch] == second.data["sha"]
    last = [p for p in state["pushes"] if p["ref"] == f"refs/heads/{branch}"][-1]
    assert (last["before"], last["forced"]) == (head, False)  # a fast forward
    comments = [c["body"] for c in state["issue_comments"] if c["number"] == number]
    assert comments == [f"Pushed {second.data['sha'][:7]}: Add a docstring"]


async def test_the_pusher_refuses_unsafe_pushes(setup: Setup) -> None:
    bob = await prepared(setup)
    await fixed(bob)
    change, data = await exported(setup, bob)
    base = data["base_sha"]
    key = {"_idempotency_key": f"task:{bob.ctx.task_id}:action:9"}
    for branch in ("main", "feature/x", "agent/../main", "agent/x.lock", "agent/"):
        refused = await bob.run(
            "github_push_changes", {**push_args(change, base, branch), **key}, approved=True
        )
        assert refused.is_error, branch
        assert "not a branch under agent/" in refused.text
    # The blocking checks run again just before pushing: a workflow file never leaves.
    sneaky = {**data, "files": [*data["files"], {"path": ".github/workflows/ci.yml", "status": "A", "mode": "100644",
                                                 "content_b64": base64.b64encode(b"on: push\n").decode()}]}  # fmt: skip
    blocked = await bob.run("github_push_changes", {**push_args(store_change(setup, bob, sneaky), base, "agent/w"), **key},
                            approved=True)  # fmt: skip
    assert blocked.is_error
    assert "workflow file" in blocked.text
    # A change of another task, or for another base, is not this task's change.
    other = await setup.executor("bob")
    stolen = await other.run(
        "github_push_changes", {**push_args(change, base, "agent/stolen"), **key}, approved=True
    )
    assert "no such change" in stolen.text
    wrong = await bob.run(
        "github_push_changes", {**push_args(change, "f" * 40, "agent/wrong"), **key}, approved=True
    )
    assert "no such change" in wrong.text
    # Someone who can read but not push is refused, whatever they approved.
    alice = await setup.executor("alice")
    their = store_change(setup, alice, data)
    denied = await alice.run(
        "github_push_changes", {**push_args(their, base, "agent/q"), **key}, approved=True
    )
    assert denied.is_error
    assert "can't push" in denied.text
    branches = (await github_state())["refs"]
    assert not {"agent/w", "agent/stolen", "agent/wrong", "agent/q"} & set(branches)


async def test_a_push_needs_approval_in_ask_mode(setup: Setup) -> None:
    bob = await prepared(setup)
    await fixed(bob)
    change, data = await exported(setup, bob)
    asked = await bob.run("github_push_changes", {**push_args(change, data["base_sha"], "agent/ask"),
                                                  "_idempotency_key": "k-ask"})  # fmt: skip
    assert asked.decision is not None
    assert asked.decision.outcome == "needs_approval"
    assert asked.decision.tier == "reversible_write"
    assert "agent/ask" not in (await github_state())["refs"]
    auto = await setup.executor("bob", mode="auto")
    decision = await auto.policy.check_tool("github", "push_changes", None)
    assert decision.outcome == "allowed"  # Auto allows a reversible write within its ceiling


async def test_the_baseline_ignores_tool_caches(setup: Setup, stack: Stack) -> None:
    bob = await prepared(setup)
    provider = HttpSandboxProvider(f"{FAKES}/sandbox/v1", "sbx-local-key", setup.http)
    ws = await Workspaces(stack.pool, provider).find(setup.acme.id, UUID(str(bob.ctx.task_id)), REPO)
    assert ws is not None
    ran = await provider.exec(ws.sandbox_id, "mkdir -p .pytest_cache node_modules/x && touch .pytest_cache/v "
                              "node_modules/x/i.js a.pyc && git log --format=%s", cwd=ws.root)  # fmt: skip
    assert ran.stdout.strip() == "baseline"
    assert (await provider.diff(ws.sandbox_id, ws.root)).files == []

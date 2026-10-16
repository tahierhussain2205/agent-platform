"""Console repositories: members see each repository's protected paths and setup recipe; admins
edit them (paths validated, an admin's test command kept as is, an empty one back to detection);
every change is audited; nothing crosses orgs."""

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from psycopg.types.json import Jsonb

from app.auth.clerk import ClerkVerifier
from tests.connect import sql
from tests.console.conftest import ISSUER, ORIGIN, Jwks
from tests.console.test_schedules_api import ALICE, BOB, DAVE, KEY
from tests.stack import Stack, app_for


@pytest_asyncio.fixture(loop_scope="session")
async def console(stack: Stack) -> AsyncIterator[httpx.AsyncClient]:
    verifier = ClerkVerifier(
        ISSUER, "https://clerk.test/jwks", [ORIGIN],
        http=httpx.AsyncClient(transport=httpx.MockTransport(Jwks([KEY]).handler)),
    )  # fmt: skip
    app = app_for(stack, clerk_verifier=verifier)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://api"
    ) as client:
        yield client


@pytest.fixture(autouse=True)
def _reset(stack: Stack) -> Iterator[None]:
    yield
    sql(stack, "update app.repositories set protected_paths = '{}', setup_recipe = '{}'")


def repo_id(stack: Stack, name: str = "acme/api") -> str:
    return str(sql(stack, "select id from app.repositories where full_name = %s", (name,))[0][0])


def globex_repo(stack: Stack) -> str:
    org = stack.env.world.globex.id
    rows = sql(stack, "insert into app.repositories (org_id, external_id, full_name) values (%s, 'R_globex', "
                      "'globex/payments') on conflict (org_id, provider, external_id) do update set "
                      "full_name = excluded.full_name returning id", (org,))  # fmt: skip
    return str(rows[0][0])


async def test_members_see_repositories_and_admins_edit_them(
    console: httpx.AsyncClient, stack: Stack
) -> None:
    rid = repo_id(stack)
    sql(stack, "update app.repositories set setup_recipe = %s where id = %s",
        (Jsonb({"test": "python -m pytest -q", "source": "detected", "status": "works",
                "last_run": {"exit_code": 0, "summary": "3 passed"}}), rid))  # fmt: skip
    listed = (await console.get("/api/repositories", headers=BOB)).json()
    assert listed["can_manage"] is False
    (repo,) = [r for r in listed["repositories"] if r["id"] == rid]
    assert repo["recipe"] == {"test": "python -m pytest -q", "source": "detected", "status": "works",
                              "last_run": {"exit_code": 0, "summary": "3 passed"}}  # fmt: skip
    refused = await console.patch(f"/api/repositories/{rid}", json={"protected_paths": ["x/"]}, headers=BOB)
    assert refused.status_code == 403
    changed = await console.patch(f"/api/repositories/{rid}", headers=ALICE, json={
        "protected_paths": ["/migrations/", "*.tf", "migrations/", " "], "test_command": "make  test"})  # fmt: skip
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert body["protected_paths"] == ["migrations/", "*.tf"]
    assert body["recipe"]["test"] == "make test"
    assert (body["recipe"]["source"], body["recipe"]["status"], body["recipe"]["last_run"]) == ("admin", "untested",
                                                                                                None)  # fmt: skip
    audit: list[Any] = sql(stack, "select actor_id, target, metadata from app.audit_log "
                                  "where action = 'repository.updated' order by occurred_at desc limit 1")  # fmt: skip
    assert audit[0][1] == "acme/api"
    assert audit[0][2]["fields"] == ["protected_paths", "test_command"]
    # An empty command goes back to detection.
    cleared = await console.patch(f"/api/repositories/{rid}", json={"test_command": ""}, headers=ALICE)
    assert cleared.json()["recipe"] == {"test": None, "source": None, "status": None, "last_run": None}


async def test_paths_are_validated(console: httpx.AsyncClient, stack: Stack) -> None:
    rid = repo_id(stack)
    for bad in (["../secrets"], ["a/../b"], ["x" * 201], ["has space"], [f"p{i}" for i in range(51)]):
        r = await console.patch(f"/api/repositories/{rid}", json={"protected_paths": bad}, headers=ALICE)
        assert r.status_code == 422, bad
    bad_command = await console.patch(
        f"/api/repositories/{rid}", json={"test_command": "a\nb"}, headers=ALICE
    )
    assert bad_command.status_code == 422
    assert sql(stack, "select protected_paths from app.repositories where id = %s", (rid,)) == [([],)]


async def test_another_orgs_repositories_are_invisible(console: httpx.AsyncClient, stack: Stack) -> None:
    theirs = globex_repo(stack)
    acme = (await console.get("/api/repositories", headers=ALICE)).json()["repositories"]
    assert theirs not in [r["id"] for r in acme]
    missing = await console.patch(
        f"/api/repositories/{theirs}", json={"protected_paths": ["x/"]}, headers=ALICE
    )
    assert missing.status_code == 404
    assert sql(stack, "select protected_paths from app.repositories where id = %s", (theirs,)) == [([],)]
    dave = (await console.get("/api/repositories", headers=DAVE)).json()["repositories"]
    assert [r["full_name"] for r in dave] == ["globex/payments"]

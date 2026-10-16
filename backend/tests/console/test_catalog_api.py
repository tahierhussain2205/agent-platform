"""Console catalog: members read it; admins create, edit, confirm and archive services and start
proposals (one run per org at a time); owners and channels are replaced as sent while proposal
owners keep their reasons; every change is audited; nothing crosses orgs."""

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from app.auth.clerk import ClerkVerifier
from tests.connect import sql
from tests.console.conftest import ISSUER, ORIGIN, Jwks
from tests.console.test_schedules_api import ALICE, BOB, DAVE, KEY, channel
from tests.stack import Stack, app_for

NAMES = ("checkout", "Checkout", "refunds", "web-app")


@pytest_asyncio.fixture(loop_scope="session")
async def console(stack: Stack) -> AsyncIterator[httpx.AsyncClient]:
    verifier = ClerkVerifier(
        ISSUER,
        "https://clerk.test/jwks",
        [ORIGIN],
        http=httpx.AsyncClient(transport=httpx.MockTransport(Jwks([KEY]).handler)),
    )
    app = app_for(stack, clerk_verifier=verifier)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://api"
    ) as client:
        yield client


@pytest.fixture(autouse=True)
def _clean(stack: Stack) -> Iterator[None]:
    def clean() -> None:
        sql(stack, "delete from app.services where lower(name) = any(%s)", ([n.lower() for n in NAMES],))
        stack.temporal.started.clear()
        stack.temporal.results.clear()
        stack.temporal.fail = False

    clean()
    yield
    clean()


def member(stack: Stack, email: str) -> str:
    return str(
        sql(
            stack,
            "select m.id from app.org_memberships m join app.users u on u.id = m.user_id where u.email = %s",
            (email,),
        )[0][0]
    )


def audits(stack: Stack, action: str) -> list[Any]:
    rows: list[Any] = sql(
        stack,
        "select actor_id, target, metadata from app.audit_log where action = %s order by occurred_at",
        (action,),
    )
    return rows


async def create(console: httpx.AsyncClient, who: Any = ALICE, **body: Any) -> httpx.Response:
    return await console.post("/api/catalog", json={"name": "checkout", **body}, headers=who)


async def test_members_read_the_catalog_and_only_admins_change_it(
    console: httpx.AsyncClient, stack: Stack
) -> None:
    made = await create(console, owners=[member(stack, "bob@acme.test")])
    assert made.status_code == 201, made.text
    service = made.json()
    assert (service["status"], service["confirmed_by"]) == ("active", "alice@acme.test")
    listed = (await console.get("/api/catalog", headers=BOB)).json()
    assert listed["can_manage"] is False
    assert "checkout" in [s["name"] for s in listed["services"]]
    assert (await console.get("/api/catalog", headers=ALICE)).json()["can_manage"] is True
    sid = service["id"]
    for method, path, body in (
        ("POST", "/api/catalog", {"name": "refunds"}),
        ("PATCH", f"/api/catalog/{sid}", {"description": "mine now"}),
        ("POST", f"/api/catalog/{sid}/confirm", None),
        ("POST", f"/api/catalog/{sid}/archive", None),
        ("POST", "/api/catalog/propose", None),
        ("GET", "/api/catalog/options", None),
        ("GET", "/api/catalog/proposal", None),
    ):
        refused = await console.request(method, path, json=body, headers=BOB)
        assert refused.status_code == 403, (method, path, refused.text)
    assert not stack.temporal.started
    assert sql(stack, "select description, status from app.services where id = %s", (sid,)) == [
        (None, "active")
    ]


async def test_create_validates_and_names_are_unique(console: httpx.AsyncClient, stack: Stack) -> None:
    repo = sql(stack, "select id from app.repositories where full_name = 'acme/api'")[0][0]
    made = await create(
        console,
        description="Cart and checkout",
        repository_id=str(repo),
        jira_project="pay",
        jira_component="Checkout",
        paths=["/src/pricing/", "/src/pricing/", " "],
        owners=[member(stack, "bob@acme.test")],
        channels=[channel(stack, "bugs")],
    )
    assert made.status_code == 201, made.text
    s = made.json()
    assert s["repository"] == {"id": str(repo), "full_name": "acme/api"}
    assert (s["jira_project"], s["jira_component"], s["paths"]) == ("PAY", "Checkout", ["/src/pricing/"])
    assert [(o["email"], o["source"]) for o in s["owners"]] == [("bob@acme.test", "manual")]
    assert [c["name"] for c in s["channels"]] == ["bugs"]
    assert audits(stack, "catalog.created")[-1][1] == "checkout"
    taken = await create(console, name=" Checkout ")
    assert (taken.status_code, taken.json()["error"]["code"]) == (409, "name_taken")
    for bad in ({"name": "  "}, {"jira_project": "pay-1"}, {"paths": ["../etc"]}, {"owners": ["x"]}):
        assert (await create(console, **{"name": "refunds", **bad})).status_code == 422, bad


async def test_edits_replace_owners_and_channels_but_keep_proposal_reasons(
    console: httpx.AsyncClient, stack: Stack
) -> None:
    bob, quinn, alice = (member(stack, f"{n}@acme.test") for n in ("bob", "quinn", "alice"))
    sid = (await create(console, owners=[alice], channels=[channel(stack, "bugs")])).json()["id"]
    sql(stack, "insert into app.service_owners (org_id, service_id, membership_id, source, reason) select "
               "org_id, id, %s, 'codeowners', 'CODEOWNERS /src/pricing/ in acme/cart-service' from app.services "
               "where id = %s", (bob, sid))  # fmt: skip
    edited = await console.patch(
        f"/api/catalog/{sid}",
        json={"owners": [bob, quinn], "channels": [channel(stack, "general")], "jira_component": ""},
        headers=ALICE,
    )
    assert edited.status_code == 200, edited.text
    owners = {o["email"]: (o["source"], o["reason"]) for o in edited.json()["owners"]}
    assert owners == {
        "bob@acme.test": ("codeowners", "CODEOWNERS /src/pricing/ in acme/cart-service"),  # kept as found
        "quinn@acme.test": ("manual", None),
    }
    assert [c["name"] for c in edited.json()["channels"]] == ["general"]
    assert edited.json()["jira_component"] is None
    assert audits(stack, "catalog.updated")[-1][2]["fields"] == ["channels", "jira_component", "owners"]
    renamed = await console.patch(f"/api/catalog/{sid}", json={"name": "refunds"}, headers=ALICE)
    assert renamed.json()["name"] == "refunds"
    assert (await console.patch(f"/api/catalog/{sid}", json={"name": ""}, headers=ALICE)).status_code == 422


async def test_confirm_and_archive(console: httpx.AsyncClient, stack: Stack) -> None:
    acme = stack.env.world.acme
    sid = str(sql(stack, "insert into app.services (org_id, name, status, evidence) values (%s, 'web-app', "
                         "'proposed', '[\"CODEOWNERS * in acme/web-app\"]') returning id", (acme.id,))[0][0])  # fmt: skip
    listed = (await console.get("/api/catalog", headers=ALICE)).json()["services"]
    assert listed[0]["id"] == sid  # proposals first
    assert listed[0]["evidence"] == ["CODEOWNERS * in acme/web-app"]
    confirmed = await console.post(f"/api/catalog/{sid}/confirm", headers=ALICE)
    assert (confirmed.json()["status"], confirmed.json()["confirmed_by"]) == ("active", "alice@acme.test")
    assert audits(stack, "catalog.confirmed")[-1][1] == "web-app"
    assert (await console.post(f"/api/catalog/{sid}/archive", headers=ALICE)).json()["status"] == "archived"
    assert sid not in [s["id"] for s in (await console.get("/api/catalog", headers=ALICE)).json()["services"]]
    assert (await console.post(f"/api/catalog/{sid}/confirm", headers=ALICE)).status_code == 404
    assert (
        await console.patch(f"/api/catalog/{sid}", json={"description": "x"}, headers=ALICE)
    ).status_code == 404
    # The name is free again for a new service.
    assert (await create(console, name="web-app")).status_code == 201


async def test_nothing_crosses_orgs(console: httpx.AsyncClient, stack: Stack) -> None:
    sid = (await create(console)).json()["id"]
    assert sid not in [s["id"] for s in (await console.get("/api/catalog", headers=DAVE)).json()["services"]]
    for method, path in (("PATCH", f"/api/catalog/{sid}"), ("POST", f"/api/catalog/{sid}/archive")):
        refused = await console.request(method, path, json={"description": "globex"}, headers=DAVE)
        assert refused.status_code == 404, (method, refused.text)
    # Another org's member or channel cannot be attached.
    dave = member(stack, "dave@globex.test")
    edited = await console.patch(f"/api/catalog/{sid}", json={"owners": [dave]}, headers=ALICE)
    assert edited.json()["owners"] == []
    foreign = await console.patch(
        f"/api/catalog/{sid}", json={"channels": [channel(stack, "general", "org_globex")]}, headers=ALICE
    )
    assert foreign.status_code in (403, 409, 422)
    assert sql(stack, "select count(*) from app.service_channels where service_id = %s", (sid,)) == [(0,)]
    options = (await console.get("/api/catalog/options", headers=ALICE)).json()
    assert "dave@globex.test" not in [m["email"] for m in options["members"]]


async def test_proposals_start_once_and_report_back(console: httpx.AsyncClient, stack: Stack) -> None:
    acme = stack.env.world.acme
    assert (await console.get("/api/catalog/proposal", headers=ALICE)).json() == {"status": "none"}
    started = await console.post("/api/catalog/propose", headers=ALICE)
    assert (started.status_code, started.json()) == (202, {"status": "running"})
    (workflow, args, workflow_id) = stack.temporal.started[0]
    assert (workflow, args, workflow_id) == (
        "CatalogProposalWorkflow",
        [str(acme.id), str(acme.admin)],
        f"catalog-propose-{acme.id}",
    )
    again = await console.post("/api/catalog/propose", headers=ALICE)
    assert (again.status_code, again.json()["error"]["code"]) == (409, "already_running")
    assert (await console.get("/api/catalog/proposal", headers=ALICE)).json() == {"status": "running"}
    stack.temporal.results[workflow_id] = {"proposed": 3, "refreshed": 0, "linked": 1, "kept": 0, "skipped": 0,
                                           "notes": []}  # fmt: skip
    done = (await console.get("/api/catalog/proposal", headers=ALICE)).json()
    assert (done["status"], done["proposed"], done["linked"]) == ("completed", 3, 1)
    assert (await console.post("/api/catalog/propose", headers=ALICE)).status_code == 202  # a new run
    assert len(audits(stack, "catalog.propose_requested")) >= 2
    stack.temporal.fail = True
    stack.temporal.results[workflow_id] = {}
    down = await console.post("/api/catalog/propose", headers=ALICE)
    assert (down.status_code, down.json()["error"]["code"]) == (503, "unavailable")


async def test_log_sources_are_set_by_admins_and_validated(console: httpx.AsyncClient, stack: Stack) -> None:
    sid = (
        await create(console, log_source={"project": "acme-prod", "resource_type": "cloud_run_revision"})
    ).json()["id"]
    listed = next(
        s for s in (await console.get("/api/catalog", headers=BOB)).json()["services"] if s["id"] == sid
    )
    assert listed["log_source"] == {"provider": "google_cloud_logging", "project": "acme-prod",
                                    "resource_type": "cloud_run_revision", "label": "service_name"}  # fmt: skip
    for bad in (
        {"project": "Acme Prod"},  # not a project id
        {"project": "acme-prod", "value": 'x" OR severity>=DEBUG'},  # would leave the filter's literal
        {"project": "acme-prod", "label": "labels.x"},
        {"project": "acme-prod", "provider": "datadog"},
    ):
        refused = await console.patch(f"/api/catalog/{sid}", json={"log_source": bad}, headers=ALICE)
        assert refused.status_code == 422, bad
    cleared = await console.patch(f"/api/catalog/{sid}", json={"log_source": None}, headers=ALICE)
    assert cleared.json()["log_source"] is None
    assert (await console.patch(f"/api/catalog/{sid}", json={"log_source": {"project": "acme-prod"}},
                                headers=BOB)).status_code == 403  # fmt: skip

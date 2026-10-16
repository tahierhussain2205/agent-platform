"""The service catalog: ranking owners for a problem (``catalog_find_owners``) and proposing services
from GitHub and Jira within the proposing admin's access, without overwriting an admin's work."""

import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio

from app.agent.runner import AgentDeps
from app.catalog import Owner, Proposal, build, matches, save
from app.tools.catalog import rank, words
from app.workflows.catalog import CatalogActivities
from tests.connect import connect_github, connect_jira, enable, link_github, sql
from tests.stack import PROVIDER_SETTINGS, Stack, fakes_up
from tests.tools.test_integrations import Setup

pytestmark = pytest.mark.skipif(not fakes_up(), reason="stand-ins not reachable (run `make infra`)")
OURS = ("cart-service", "web-app", "infra", "Refunds", "Webhooks", "Checkout")


def member(stack: Stack, email: str) -> uuid.UUID:
    rows = sql(
        stack,
        "select m.id from app.org_memberships m join app.users u on u.id = m.user_id "
        "where m.org_id = %s and u.email = %s",
        (stack.env.world.acme.id, email),
    )
    return rows[0][0]  # type: ignore[no-any-return]


def forget_services(stack: Stack) -> None:
    sql(
        stack,
        "delete from app.services where org_id = %s and name = any(%s)",
        (stack.env.world.acme.id, list(OURS)),
    )


def service(stack: Stack, name: str) -> dict[str, Any]:
    rows = sql(
        stack,
        "select s.id, s.status, s.description, r.full_name, s.jira_project, s.jira_component, s.paths, "
        "s.evidence from app.services s left join app.repositories r on r.id = s.repository_id "
        "where s.org_id = %s and s.name = %s and s.status <> 'archived'",
        (stack.env.world.acme.id, name),
    )
    if not rows:
        return {}
    keys = (
        "id",
        "status",
        "description",
        "repository",
        "jira_project",
        "jira_component",
        "paths",
        "evidence",
    )
    found = dict(zip(keys, rows[0], strict=True))
    found["owners"] = {
        email: (source, reason)
        for email, source, reason in sql(
            stack,
            "select u.email, so.source, so.reason from app.service_owners so join app.org_memberships m "
            "on m.id = so.membership_id join app.users u on u.id = m.user_id where so.service_id = %s",
            (found["id"],),
        )
    }
    return found


@pytest_asyncio.fixture(loop_scope="session")
async def setup(stack: Stack) -> AsyncIterator[Setup]:
    acme = stack.env.world.acme
    for key in ("slack", "jira", "github"):
        enable(stack, acme, key)
    await connect_jira(stack, acme, "acc-alice")
    await connect_github(stack, acme)
    await link_github(stack, acme, acme.admin, "alice-gh")
    await link_github(stack, acme, acme.user, "bob-gh")
    await link_github(stack, acme, member(stack, "quinn@acme.test"), "quinn-gh")
    forget_services(stack)
    async with httpx.AsyncClient(timeout=30) as http:
        yield Setup(stack, http)
    forget_services(stack)


def activities(setup: Setup) -> CatalogActivities:
    s = setup.stack
    deps = AgentDeps(
        s.pool, s.vault, s.slack, s.kv, cast(Any, None), settings=PROVIDER_SETTINGS, http=setup.http
    )
    return CatalogActivities(deps)


# ------------------------------------------------------------------ ranking (no database)


def test_words_drop_stopwords_short_words_and_plurals() -> None:
    assert words("Checkout fails for carts over 20 items") == {"checkout", "cart", "item"}
    assert words("cart-service") == {"cart"}
    assert words(None) == set()


def test_rank_weighs_component_repository_path_and_names() -> None:
    services = [
        {"id": 1, "name": "cart-service", "description": "Prices carts", "jira_project": "PAY",
         "jira_component": "Checkout", "paths": ["/src/pricing/"], "repository": "acme/cart-service"},
        {"id": 2, "name": "web-app", "description": "The customer facing website", "jira_project": "WEB",
         "jira_component": "Search", "paths": ["/search/"], "repository": "acme/web-app"},
        {"id": 3, "name": "Refunds", "description": "Refund processing for carts", "jira_project": "PAY",
         "jira_component": "Refunds", "paths": [], "repository": None},
    ]  # fmt: skip
    best = rank(services, {"text": "Checkout fails for carts over 20 items"})
    assert [(m.name, m.score) for m in best] == [("cart-service", 4), ("Refunds", 1)]
    assert best[0].why == ["mentions cart, checkout"]
    exact = rank(services, {"component": "search", "repository": "ACME/web-app", "path": "search/index.ts"})
    assert exact[0].name == "web-app"
    assert exact[0].score == 12
    assert exact[0].why == ["Jira component Search", "repository acme/web-app", "path search/index.ts"]
    assert rank(services, {"text": "the and for"}) == []
    assert [m.name for m in rank(services, {"project": "PAY"})] == ["Refunds", "cart-service"]  # ties by name


def test_a_component_matches_by_name_description_or_codeowners_directory() -> None:
    cart = Proposal("cart-service", repository={"id": "1", "full_name": "acme/cart-service"})
    web = Proposal("web-app", repository={"id": "2", "full_name": "acme/web-app"}, segments={"search"})
    assert matches({"name": "Cart"}, cart)
    assert matches({"name": "Checkout", "description": "Cart and checkout (cart-service)"}, cart)
    assert matches({"name": "Search", "description": "Site search"}, web)
    assert not matches({"name": "Refunds", "description": "Refund processing"}, cart)
    assert not matches({"name": "Cart"}, Proposal("Cart"))  # a proposal without a repository


def test_an_owner_found_twice_keeps_both_reasons() -> None:
    p = Proposal("cart-service")
    p.add_owner(Owner("m1", "codeowners", "CODEOWNERS /src/pricing/ in acme/cart-service"))
    p.add_owner(Owner("m1", "component_lead", "lead of Jira component PAY/Checkout"))
    p.add_owner(Owner("m1", "component_lead", "lead of Jira component PAY/Checkout"))
    assert [(o.source, o.reason) for o in p.owners] == [
        ("codeowners", "CODEOWNERS /src/pricing/ in acme/cart-service; lead of Jira component PAY/Checkout")
    ]


# ------------------------------------------------------------------ find_owners


async def test_find_owners_uses_confirmed_services_and_explains_why(setup: Setup) -> None:
    stack, acme = setup.stack, setup.acme
    quinn = member(stack, "quinn@acme.test")
    repo = sql(stack, "insert into app.repositories (org_id, external_id, full_name) values (%s, 'R_cart', "
                      "'acme/cart-service') on conflict (org_id, provider, external_id) do update set full_name = "
                      "excluded.full_name returning id", (acme.id,))[0][0]  # fmt: skip
    cart = sql(stack, "insert into app.services (org_id, name, repository_id, jira_project, jira_component, paths) "
                      "values (%s, 'cart-service', %s, 'PAY', 'Checkout', '{/src/pricing/}') returning id",
               (acme.id, repo))[0][0]  # fmt: skip
    sql(stack, "insert into app.service_owners (org_id, service_id, membership_id, source, reason) values "
               "(%s, %s, %s, 'codeowners', 'CODEOWNERS /src/pricing/ in acme/cart-service')",
        (acme.id, cart, acme.user))  # fmt: skip
    proposed = sql(stack, "insert into app.services (org_id, name, status, jira_component) values "
                          "(%s, 'Checkout', 'proposed', 'Checkout') returning id", (acme.id,))[0][0]  # fmt: skip
    sql(stack, "insert into app.service_owners (org_id, service_id, membership_id) values (%s, %s, %s)",
        (acme.id, proposed, quinn))  # fmt: skip
    try:
        bob = await setup.executor("bob")
        found = await bob.run(
            "catalog_find_owners", {"text": "Checkout fails for carts over 20 items", "component": "Checkout"}
        )
        assert not found.is_error, found.text
        assert found.decision is not None
        assert found.decision.outcome == "allowed"  # always on, a read
        assert [c["email"] for c in found.data["candidates"]] == ["bob@acme.test"]  # not the proposal's quinn
        assert found.data["candidates"][0]["reasons"] == [
            "owner of cart-service (CODEOWNERS /src/pricing/ in acme/cart-service)"
        ]
        assert "[S1] cart-service (matched on Jira component Checkout; mentions cart, checkout)" in found.text
        assert bob.ctx.refs.by_ref["S1"].ref == f"catalog:{cart}"
        sql(stack, "update app.org_memberships set status = 'removed' where id = %s", (acme.user,))
        try:  # a member who left is no owner
            gone = await bob.run("catalog_find_owners", {"component": "Checkout"})
            assert gone.data["candidates"] == []
            assert "no owners recorded" in gone.text
        finally:
            sql(stack, "update app.org_memberships set status = 'active' where id = %s", (acme.user,))
        none = await bob.run("catalog_find_owners", {"text": "the printer on floor three"})
        assert none.data["candidates"] == []
        assert "No confirmed service" in none.text
    finally:
        forget_services(stack)


async def test_find_owners_never_reads_another_orgs_catalog(setup: Setup) -> None:
    globex = setup.stack.env.world.globex
    sql(setup.stack, "update app.services set jira_component = 'Checkout' where org_id = %s", (globex.id,))
    try:
        found = await (await setup.executor("bob")).run("catalog_find_owners", {"component": "Checkout"})
        assert found.data["candidates"] == []
    finally:
        sql(setup.stack, "update app.services set jira_component = null where org_id = %s", (globex.id,))


# ------------------------------------------------------------------ proposals


async def test_only_an_active_admin_proposes(setup: Setup) -> None:
    acts = activities(setup)
    with pytest.raises(PermissionError):
        await acts._executor(str(setup.acme.id), str(setup.acme.user))
    with pytest.raises(PermissionError):  # another org's admin
        await acts._executor(str(setup.acme.id), str(setup.stack.env.world.globex.admin))


async def test_proposals_from_codeowners_and_jira_components(setup: Setup) -> None:
    stack, acme = setup.stack, setup.acme
    acts = activities(setup)
    result = await acts.propose(str(acme.id), str(acme.admin))
    assert result["proposed"] == 5, result
    assert any("acme/infra" in e for e in service(stack, "infra")["evidence"])

    cart = service(stack, "cart-service")
    assert cart["status"] == "proposed"
    assert cart["repository"] == "acme/cart-service"
    assert (cart["jira_project"], cart["jira_component"]) == (
        "PAY",
        "Checkout",
    )  # its description names the repo
    assert cart["paths"] == ["/src/pricing/", "/docs/"]
    assert cart["owners"] == {
        "bob@acme.test": (
            "codeowners", "CODEOWNERS /src/pricing/ in acme/cart-service; lead of Jira component PAY/Checkout"
        ),
        "quinn@acme.test": ("codeowners", "CODEOWNERS /docs/ in acme/cart-service"),
    }  # fmt: skip
    assert (
        "CODEOWNERS * in acme/cart-service: @acme/platform (no member with this linked GitHub account)"
        in (cart["evidence"])
    )

    web = service(stack, "web-app")
    assert (web["jira_project"], web["jira_component"]) == ("WEB", "Search")  # /search/ in its CODEOWNERS
    assert set(web["owners"]) == {"alice@acme.test", "quinn@acme.test"}
    assert (
        web["owners"]["quinn@acme.test"][1]
        == "CODEOWNERS * in acme/web-app; lead of Jira component WEB/Search"
    )

    refunds = service(stack, "Refunds")  # no repository matched: a service of its own
    assert (refunds["repository"], refunds["jira_component"]) == (None, "Refunds")
    assert refunds["description"] == "Refund processing"
    assert refunds["owners"] == {"alice@acme.test": ("component_lead", "lead of Jira component PAY/Refunds")}
    assert service(stack, "Webhooks")["evidence"] == ["Jira component PAY/Webhooks has no lead"]

    audit = sql(stack, "select actor_type, actor_id, metadata from app.audit_log where org_id = %s and "
                       "action = 'catalog.proposed'", (acme.id,))  # fmt: skip
    assert audit[-1][0:2] == ("agent", acme.admin)
    assert audit[-1][2]["proposed"] == 5
    reads = sql(stack, "select count(*) from app.audit_log where org_id = %s and action like 'policy.%%' and "
                       "target like 'github.get_codeowners%%'", (acme.id,))[0][0]  # fmt: skip
    assert reads >= 3  # every read went through the policy module

    again = await acts.propose(str(acme.id), str(acme.admin))
    assert (again["proposed"], again["refreshed"]) == (0, 5)
    assert service(stack, "cart-service")["owners"] == cart["owners"]  # no duplicates


async def test_an_admins_work_is_never_overwritten(setup: Setup) -> None:
    stack, acme = setup.stack, setup.acme
    acts = activities(setup)
    forget_services(stack)
    await acts.propose(str(acme.id), str(acme.admin))
    cart = service(stack, "cart-service")
    sql(stack, "update app.services set status = 'active', confirmed_by = %s, confirmed_at = now(), "
               "description = 'Prices carts' where id = %s", (acme.admin, cart["id"]))  # fmt: skip
    sql(stack, "delete from app.service_owners where service_id = %s and membership_id = %s",
        (cart["id"], member(stack, "quinn@acme.test")))  # fmt: skip
    sql(stack, "insert into app.service_owners (org_id, service_id, membership_id, source, reason) values "
               "(%s, %s, %s, 'manual', 'tech lead') on conflict do nothing",
        (acme.id, cart["id"], acme.admin))  # fmt: skip
    webhooks = service(stack, "Webhooks")
    sql(stack, "update app.services set status = 'archived' where id = %s", (webhooks["id"],))
    refunds = service(stack, "Refunds")
    sql(stack, "insert into app.service_owners (org_id, service_id, membership_id, source) values "
               "(%s, %s, %s, 'manual')", (acme.id, refunds["id"], acme.user))  # fmt: skip
    web = service(stack, "web-app")  # an admin made web-app by hand, without links
    sql(stack, "delete from app.services where id = %s", (web["id"],))
    sql(
        stack, "insert into app.services (org_id, name, status) values (%s, 'Web-App ', 'active')", (acme.id,)
    )

    result = await acts.propose(str(acme.id), str(acme.admin))
    assert (result["kept"], result["linked"], result["skipped"]) == (1, 1, 1), result

    kept = service(stack, "cart-service")
    assert kept["status"] == "active"
    assert kept["description"] == "Prices carts"
    assert set(kept["owners"]) == {"alice@acme.test", "bob@acme.test"}  # quinn stays removed
    assert kept["owners"]["alice@acme.test"] == ("manual", "tech lead")
    assert service(stack, "Webhooks") == {}  # archived: not proposed again
    assert "bob@acme.test" in service(stack, "Refunds")["owners"]  # a manual owner survives a refresh
    linked = sql(stack, "select r.full_name, s.jira_component, s.paths, (select count(*) from app.service_owners o "
                        "where o.service_id = s.id) from app.services s join app.repositories r on r.id = "
                        "s.repository_id where s.org_id = %s and s.name = 'Web-App '", (acme.id,))  # fmt: skip
    assert linked == [("acme/web-app", "Search", ["/search/"], 0)]  # links filled, owners left to the admin


async def test_proposals_stay_within_the_admins_access(setup: Setup) -> None:
    stack, acme = setup.stack, setup.acme
    forget_services(stack)
    sql(stack, "update app.connections set status = 'revoked' where org_id = %s and integration_key = 'github' "
               "and membership_id = %s", (acme.id, acme.admin))  # fmt: skip
    try:
        acts = activities(setup)
        executor = await acts._executor(str(acme.id), str(acme.admin))
        proposals, _ = await build(executor, stack.pool, acme.id)
        cart = next(p for p in proposals if p.name == "cart-service")
        # without her GitHub link no CODEOWNERS is read for her; Jira still says who leads Checkout
        assert [o.source for o in cart.owners] == ["component_lead"]
        assert any("Link your GitHub account" in e for e in cart.evidence)
        counts = await save(stack.pool, acme.id, [])
        assert counts == {"proposed": 0, "refreshed": 0, "linked": 0, "kept": 0, "skipped": 0}
    finally:
        await link_github(stack, acme, acme.admin, "alice-gh")

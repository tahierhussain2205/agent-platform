"""Console roles: structured profiles and previews (D-202)."""

from dataclasses import asdict

import httpx
import psycopg

from app.workflows.roles import PreviewRequest
from tests.console.conftest import SigningKey, auth
from tests.stack import FakeTemporal
from tests.world import World

ALICE = ("user_alice", "org_acme")  # admin
BOB = ("user_bob", "org_acme")  # member
DAVE = ("user_dave", "org_globex")  # another org's admin


async def test_profiles_are_validated_saved_and_listed(
    console: httpx.AsyncClient, signing_key: SigningKey, module_world: World
) -> None:
    alice = auth(signing_key, *ALICE)
    profile = {"detail": "brief", "style": "business", "format": "summary_first", "focus": ["risks", "dates"]}
    created = await console.post("/api/roles", json={"title": "Exec", "profile": profile}, headers=alice)
    assert created.status_code == 201
    role = created.json()
    assert role["profile"] == profile

    for bad in ({"detail": "endless"}, {"tone": "pirate"}, {"focus": ["gossip"]}):
        refused = await console.patch(f"/api/roles/{role['id']}", json={"profile": bad}, headers=alice)
        assert refused.status_code == 422, bad
    changed = await console.patch(
        f"/api/roles/{role['id']}", json={"profile": {"detail": "thorough"}}, headers=alice
    )
    assert changed.json()["profile"] == {"detail": "thorough", "focus": []}  # unset choices are not stored
    listed = {
        r["title"]: r for r in (await console.get("/api/roles", headers=auth(signing_key, *BOB))).json()
    }
    assert listed["Exec"]["profile"]["detail"] == "thorough"
    with psycopg.connect(module_world.url) as conn:
        fields = conn.execute(
            "select metadata->'fields' from app.audit_log where action = 'role.updated' and target = 'Exec'"
        ).fetchall()
    assert fields == [(["profile"],)]


async def test_previews_run_on_the_worker_for_admins_of_the_org(
    console: httpx.AsyncClient, signing_key: SigningKey, temporal: FakeTemporal, module_world: World
) -> None:
    alice = auth(signing_key, *ALICE)
    body = {"title": "Support", "profile": {"detail": "brief"}, "sample": "status"}
    assert (
        await console.post("/api/roles/preview", json=body, headers=auth(signing_key, *BOB))
    ).status_code == 403
    assert (
        await console.post("/api/roles/preview", json={**body, "profile": {"detail": "x"}}, headers=alice)
    ).status_code == 422

    started = await console.post("/api/roles/preview", json=body, headers=alice)
    assert started.status_code == 202
    preview_id = started.json()["id"]
    assert preview_id.startswith(f"role-preview-{module_world.acme.id}-")
    assert started.json()["request"] == "Where do things stand, and what should happen next?"
    workflow, arg, _ = temporal.started[-1]
    assert workflow == "RolePreviewWorkflow"
    assert isinstance(arg, PreviewRequest)
    assert asdict(arg)["profile"] == {"detail": "brief", "focus": []}
    assert arg.membership_id == str(module_world.acme.admin)

    own = await console.post("/api/roles/preview", json={**body, "request": "  Any risks?  "}, headers=alice)
    assert own.json()["request"] == "Any risks?"

    assert (await console.get(f"/api/roles/preview/{preview_id}", headers=alice)).json() == {
        "status": "running"
    }
    temporal.results[preview_id] = {"without": "plain", "with_role": "short", "stopped": None}
    assert (await console.get(f"/api/roles/preview/{preview_id}", headers=alice)).json() == {
        "status": "done",
        "without": "plain",
        "with_role": "short",
    }
    # Another org's admin cannot read it, and an unknown id is not found.
    dave = auth(signing_key, *DAVE)
    assert (await console.get(f"/api/roles/preview/{preview_id}", headers=dave)).status_code == 404
    unknown = f"role-preview-{module_world.acme.id}-0000"
    assert (await console.get(f"/api/roles/preview/{unknown}", headers=alice)).status_code == 404

    temporal.fail = True
    try:
        assert (await console.post("/api/roles/preview", json=body, headers=alice)).status_code == 503
    finally:
        temporal.fail = False


async def test_limits_name_known_playbooks_and_operations(
    console: httpx.AsyncClient, signing_key: SigningKey, module_world: World
) -> None:
    alice = auth(signing_key, *ALICE)
    options = (await console.get("/api/roles/options", headers=auth(signing_key, *BOB))).json()
    assert {"jira_tickets", "implement_change"} <= {p["key"] for p in options["playbooks"]}
    ops = {o["key"]: o for o in options["operations"]}
    assert ops["github.push_changes"]["tier"] == "reversible_write"
    assert ops["jira.search_issues"]["tier"] == "read"
    assert "jira.transition_issue" not in ops  # internal: follows the change, not asked for

    limits = {
        "title": "Support",
        "allowed_playbooks": ["slack_qa", "jira_tickets", "slack_qa"],
        "blocked_operations": ["github.push_changes"],
        "ask_operations": ["jira.create_issue"],
        "max_mode": "ask",
    }
    created = (await console.post("/api/roles", json=limits, headers=alice)).json()
    assert created["allowed_playbooks"] == ["jira_tickets", "slack_qa"]
    assert created["blocked_operations"] == ["github.push_changes"]
    assert created["max_mode"] == "ask"

    for bad in (
        {"allowed_playbooks": ["write_poetry"]},
        {"blocked_operations": ["jira.delete_everything"]},
        {"ask_operations": ["jira.search_issues"]},  # reads never ask
        {"approves_operations": ["slack.read_channel"]},
        {"max_mode": "auto"},
        {"title": None},
    ):
        refused = await console.patch(f"/api/roles/{created['id']}", json=bad, headers=alice)
        assert refused.status_code == 422, bad
    cleared = await console.patch(
        f"/api/roles/{created['id']}", json={"allowed_playbooks": None, "max_mode": None}, headers=alice
    )
    assert cleared.json()["allowed_playbooks"] is None
    assert cleared.json()["max_mode"] is None
    assert cleared.json()["blocked_operations"] == ["github.push_changes"]  # untouched
    lead = await console.post(
        "/api/roles",
        json={"title": "Tech Lead", "approves_operations": ["github.push_changes"]},
        headers=alice,
    )
    assert lead.json()["approves_operations"] == ["github.push_changes"]

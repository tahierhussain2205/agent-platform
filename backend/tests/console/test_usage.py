"""Console usage page API: per day, person, playbook and model, within the org only."""

import uuid

import httpx
import psycopg
import pytest

from tests.console.conftest import SigningKey, auth
from tests.world import World

ALICE = ("user_alice", "org_acme")
BOB = ("user_bob", "org_acme")
DAVE = ("user_dave", "org_globex")


def event(world: World, org: object, membership: object, model: str, tokens: int, cost: float,
          *, task: object = None, days_ago: int = 0) -> None:  # fmt: skip
    with psycopg.connect(world.url) as conn:
        conn.execute(
            "insert into app.usage_events (org_id, membership_id, task_id, kind, model, input_tokens, output_tokens, "
            "cached_tokens, cost, occurred_at) values (%s, %s, %s, 'llm', %s, %s, %s, 7, %s, "
            "now() - make_interval(days => %s))",
            (org, membership, task, model, tokens - tokens // 4, tokens // 4, cost, days_ago),
        )


async def test_usage_per_day_person_playbook_and_model(
    console: httpx.AsyncClient, signing_key: SigningKey, module_world: World
) -> None:
    acme, globex = module_world.acme, module_world.globex
    model = f"model-{uuid.uuid4().hex[:6]}"
    event(module_world, acme.id, acme.user, model, 1000, 0.5, task=acme.ids["tasks"])
    event(module_world, acme.id, acme.user, model, 400, 0.25)
    event(module_world, acme.id, acme.user, model, 9000, 4.0, days_ago=40)  # outside 30 days
    event(module_world, globex.id, globex.user, f"globex-{model}", 5000, 2.0)
    body = (await console.get("/api/usage", headers=auth(signing_key, *ALICE))).json()

    assert body["days"] == 30
    assert len(body["daily"]) == 30
    assert [d["day"] for d in body["daily"]] == sorted(d["day"] for d in body["daily"])
    assert body["daily"][-1]["tokens"] >= 1400
    assert body["today"]["tokens"] >= 1400
    assert body["month"]["tokens"] >= body["today"]["tokens"]
    assert body["limits"] == {"user_daily_token_limit": 1000000, "org_monthly_token_limit": 20000000}

    (mine,) = [m for m in body["models"] if m["model"] == model]
    assert (mine["input_tokens"], mine["output_tokens"], mine["cached_tokens"]) == (1050, 350, 14)
    assert mine["cost"] == pytest.approx(0.75)
    assert not [m for m in body["models"] if m["model"].startswith("globex-")]  # another org's usage

    bob = next(p for p in body["people"] if p["email"] == "bob@acme.test")
    assert bob["tokens"] >= 1400
    assert bob["today_tokens"] >= 1400
    assert {p["email"] for p in body["people"]} <= {"alice@acme.test", "bob@acme.test", "quinn@acme.test",
                                                    "frank@both.test", None}  # fmt: skip
    keys = {p["key"]: p for p in body["playbooks"]}
    assert keys["runbook"]["tokens"] >= 1000
    assert keys["other"]["tokens"] >= 400

    longer = (await console.get("/api/usage?days=90", headers=auth(signing_key, *ALICE))).json()
    (mine,) = [m for m in longer["models"] if m["model"] == model]
    assert mine["input_tokens"] + mine["output_tokens"] == 10400

    globex_view = (await console.get("/api/usage", headers=auth(signing_key, *DAVE))).json()
    assert not [m for m in globex_view["models"] if m["model"] == model]
    assert [m for m in globex_view["models"] if m["model"] == f"globex-{model}"]


async def test_usage_is_for_admins(console: httpx.AsyncClient, signing_key: SigningKey) -> None:
    assert (await console.get("/api/usage", headers=auth(signing_key, *BOB))).status_code == 403
    assert (await console.get("/api/usage")).status_code == 401


@pytest.mark.parametrize("days", [0, 91, -1])
async def test_usage_window_is_bounded(
    console: httpx.AsyncClient, signing_key: SigningKey, days: int
) -> None:
    response = await console.get(f"/api/usage?days={days}", headers=auth(signing_key, *ALICE))
    assert response.status_code == 422

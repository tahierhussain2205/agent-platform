"""Console autonomy ceiling: admins let their org work in Auto (spec 4.5). Without it every task runs
in Ask, whatever people choose in Slack (demo run 2: there was no way to allow Auto on staging)."""

from uuid import UUID

import httpx
import psycopg

from tests.console.conftest import SigningKey, auth
from tests.world import World, tx

ALICE = ("user_alice", "org_acme")
BOB = ("user_bob", "org_acme")
DAVE = ("user_dave", "org_globex")


def effective(world: World, membership: object, org: UUID) -> str:
    """As the API's role sees it: row level security keeps each org to its own ceiling."""
    with tx(world.url, "app_user", org) as conn:
        row = conn.execute("select app.effective_mode(%s)::text", (membership,)).fetchone()
    assert row is not None
    return str(row[0])


async def test_admins_set_the_ceiling_members_read_it(
    console: httpx.AsyncClient, signing_key: SigningKey, module_world: World
) -> None:
    acme, globex = module_world.acme, module_world.globex
    with psycopg.connect(module_world.url) as conn:
        conn.execute("delete from app.autonomy_policies where scope_type = 'org'")
        conn.execute(
            "insert into app.user_preferences (membership_id, org_id, mode) values (%s, %s, 'auto') "
            "on conflict (membership_id) do update set mode = 'auto'",
            (acme.user, acme.id),
        )
    bob = auth(signing_key, *BOB)
    assert (await console.get("/api/autonomy", headers=bob)).json() == {"org": "ask", "set": False}
    assert effective(module_world, acme.user, acme.id) == "ask"  # Bob chose Auto; the ceiling is Ask

    refused = await console.put("/api/autonomy", json={"org": "auto"}, headers=bob)
    assert refused.status_code == 403  # members read it, admins change it
    alice = auth(signing_key, *ALICE)
    assert (await console.put("/api/autonomy", json={"org": "sometimes"}, headers=alice)).status_code == 422
    allowed = await console.put("/api/autonomy", json={"org": "auto"}, headers=alice)
    assert allowed.json() == {"org": "auto", "set": True}
    assert effective(module_world, acme.user, acme.id) == "auto"
    with psycopg.connect(module_world.url) as conn:
        audited = conn.execute(
            "select metadata->>'max_mode' from app.audit_log where org_id = %s and action = 'autonomy.updated'",
            (acme.id,),
        ).fetchall()
    assert audited == [("auto",)]
    # Another org's ceiling is its own.
    assert (await console.get("/api/autonomy", headers=auth(signing_key, *DAVE))).json() == {
        "org": "ask",
        "set": False,
    }
    assert effective(module_world, globex.user, globex.id) == "ask"
    back = await console.put("/api/autonomy", json={"org": "ask"}, headers=alice)
    assert back.json() == {"org": "ask", "set": True}
    assert effective(module_world, acme.user, acme.id) == "ask"

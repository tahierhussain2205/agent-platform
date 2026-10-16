"""Console schedules: members manage their own and admins all; every save is validated against the
owner's access; pause, resume (access again, and the quota), delete and reassign are audited and
start a Temporal sync; nothing crosses orgs."""

from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any

import httpx
import pytest
import pytest_asyncio

from app.auth.clerk import ClerkVerifier
from tests.connect import enable, sql
from tests.console.conftest import ISSUER, ORIGIN, Jwks, SigningKey, mint
from tests.stack import Stack, app_for

KEY = SigningKey()


class Auth(Mapping[str, str]):
    def __init__(self, sub: str, org: str) -> None:
        self.sub, self.org = sub, org

    def __getitem__(self, key: str) -> str:
        return {"authorization": f"Bearer {mint(KEY, self.sub, self.org)}", "origin": ORIGIN}[key]

    def __iter__(self) -> Iterator[str]:
        return iter(("authorization", "origin"))

    def __len__(self) -> int:
        return 2


ALICE, BOB, QUINN, DAVE = (
    Auth("user_alice", "org_acme"),
    Auth("user_bob", "org_acme"),
    Auth("user_quinn", "org_acme"),
    Auth("user_dave", "org_globex"),
)


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
        sql(stack, "update app.schedules set status = 'deleted', pause_reason = null")
        sql(stack, "update app.org_settings set max_schedules = 25")

    clean()  # the world's own schedules too
    yield
    clean()


def channel(stack: Stack, name: str, org: str = "org_acme") -> str:
    rows = sql(
        stack,
        "select c.id from app.channels c join app.orgs o on o.id = c.org_id where o.clerk_org_id = %s "
        "and c.name = %s",
        (org, name),
    )
    return str(rows[0][0])


def body(stack: Stack, **overrides: Any) -> dict[str, Any]:
    return {
        "name": "Daily bugs",
        "kind": "channel_summary",
        "channels": [channel(stack, "bugs")],
        "cron": "0 9 * * 1-5",
        "timezone": "Europe/Berlin",
        "delivery": "dm",
        **overrides,
    }


async def create(console: httpx.AsyncClient, who: Any, data: dict[str, Any]) -> httpx.Response:
    return await console.post("/api/schedules", json=data, headers=who)


async def listing(console: httpx.AsyncClient, who: Any) -> list[dict[str, Any]]:
    response = await console.get("/api/schedules", headers=who)
    assert response.status_code == 200, response.text
    return list(response.json()["schedules"])


async def test_create_list_and_permissions(console: httpx.AsyncClient, stack: Stack) -> None:
    started = len(stack.temporal.started)
    created = await create(console, BOB, body(stack))
    assert created.status_code == 201, created.text
    s = created.json()
    assert (s["name"], s["kind"], s["status"], s["description"]) == (
        "Daily bugs", "channel_summary", "active", "Every weekday at 09:00",
    )  # fmt: skip
    assert s["channels"] == [{"id": channel(stack, "bugs"), "name": "bugs"}]
    assert len(s["next_runs"]) == 3
    assert s["next_runs"][0].endswith(("+02:00", "+01:00"))
    assert s["owner"]["email"] == "bob@acme.test"
    assert s["can_manage"]
    # A Temporal sync starts after the commit.
    assert [w for w, _, _ in stack.temporal.started[started:]] == ["ScheduleSyncWorkflow"]
    row = sql(stack, "select instruction, playbook_key, params from app.schedules where id = %s", (s["id"],))[
        0
    ]
    assert row[0] == "Channel summary of #bugs, every weekday at 09:00 (Europe/Berlin)"
    assert row[1] == "report_channel_summary"
    assert [x["id"] for x in await listing(console, BOB)] == [s["id"]]
    assert await listing(console, QUINN) == []  # members see their own
    assert [x["id"] for x in await listing(console, ALICE)] == [s["id"]]  # admins see all
    assert (await console.post(f"/api/schedules/{s['id']}/pause", headers=QUINN)).status_code == 404
    assert (await console.delete(f"/api/schedules/{s['id']}", headers=QUINN)).status_code == 404
    audit = sql(
        stack, "select action from app.audit_log where action like 'schedule.%%' order by occurred_at"
    )
    assert ("schedule.created",) in audit


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"cron": "*/10 * * * *"}, "too_frequent"),
        ({"cron": "nonsense"}, "bad_cron"),
        ({"timezone": "Nowhere/City"}, "bad_timezone"),
        ({"name": "   "}, "bad_name"),
        ({"channels": []}, "bad_channels"),
        ({"channels": ["leads-private"]}, "channel_access"),  # Bob is not a member
        ({"delivery": "channel", "delivery_channel_id": "random"}, "delivery_not_allowed"),
        ({"delivery": "channel"}, "bad_delivery"),
        ({"kind": "jira_progress", "project": "pay-1"}, "bad_project"),
    ],
)
async def test_validation_against_the_owners_access(
    console: httpx.AsyncClient, stack: Stack, overrides: dict[str, Any], code: str
) -> None:
    for key in ("channels", "delivery_channel_id"):
        value = overrides.get(key)
        if isinstance(value, list):
            overrides[key] = [channel(stack, v) for v in value]
        elif isinstance(value, str):
            overrides[key] = channel(stack, value)
    response = await create(console, BOB, body(stack, **overrides))
    assert (response.status_code, response.json()["error"]["code"]) == (422, code), response.text


async def test_jira_reports_need_jira_enabled(console: httpx.AsyncClient, stack: Stack) -> None:
    acme = stack.env.world.acme
    enable(stack, acme, "jira", enabled=False)
    data = body(stack, kind="jira_progress", project="PAY", channels=[])
    assert (await create(console, BOB, data)).json()["error"]["code"] == "jira_disabled"
    enable(stack, acme, "jira")
    created = await create(console, BOB, data)
    assert created.status_code == 201, created.text
    assert (created.json()["kind"], created.json()["project"]) == ("jira_progress", "PAY")


async def test_delivery_to_a_channel(console: httpx.AsyncClient, stack: Stack) -> None:
    data = body(stack, delivery="channel", delivery_channel_id=channel(stack, "general"))
    s = (await create(console, BOB, data)).json()
    assert s["delivery"] == {
        "type": "channel",
        "channel_id": channel(stack, "general"),
        "channel_name": "general",
    }
    edited = await console.patch(f"/api/schedules/{s['id']}", json={"delivery": "dm"}, headers=BOB)
    assert edited.json()["delivery"]["type"] == "dm"
    assert edited.json()["delivery"]["channel_id"] is None


async def test_edit_is_validated_against_the_owner_not_the_editor(
    console: httpx.AsyncClient, stack: Stack
) -> None:
    s = (await create(console, BOB, body(stack))).json()
    # Alice (an admin, a member of #leads-private) cannot widen Bob's report beyond Bob's access.
    leads = [channel(stack, "leads-private")]
    refused = await console.patch(f"/api/schedules/{s['id']}", json={"channels": leads}, headers=ALICE)
    assert refused.json()["error"]["code"] == "channel_access"
    edited = await console.patch(
        f"/api/schedules/{s['id']}", json={"cron": "30 8 * * *", "name": "Morning bugs"}, headers=ALICE
    )
    assert edited.status_code == 200, edited.text
    assert (edited.json()["description"], edited.json()["name"]) == ("Every day at 08:30", "Morning bugs")


async def test_pause_resume_and_the_quota(console: httpx.AsyncClient, stack: Stack) -> None:
    s = (await create(console, BOB, body(stack))).json()
    paused = (await console.post(f"/api/schedules/{s['id']}/pause", headers=BOB)).json()
    assert (paused["status"], paused["pause_reason"], paused["next_runs"]) == ("paused", "admin", [])
    assert paused["pause_detail"] == "paused by bob@acme.test"
    assert (await console.post(f"/api/schedules/{s['id']}/pause", headers=BOB)).status_code == 409
    other = (await create(console, BOB, body(stack, name="Second"))).json()
    sql(stack, "update app.org_settings set max_schedules = 1 where org_id = %s", (stack.env.world.acme.id,))
    over = await console.post(f"/api/schedules/{s['id']}/resume", headers=BOB)
    assert (over.status_code, over.json()["error"]["code"]) == (409, "schedule_limit")
    assert (await create(console, BOB, body(stack, name="Third"))).json()["error"]["code"] == "schedule_limit"
    await console.delete(f"/api/schedules/{other['id']}", headers=BOB)
    resumed = await console.post(f"/api/schedules/{s['id']}/resume", headers=ALICE)
    assert resumed.json()["status"] == "active"
    assert (await console.post(f"/api/schedules/{s['id']}/resume", headers=ALICE)).status_code == 409


async def test_resume_checks_access_again(console: httpx.AsyncClient, stack: Stack) -> None:
    s = (await create(console, BOB, body(stack))).json()
    bugs = channel(stack, "bugs")
    # A source channel leaving the allowlist is caught by the next run; only a delivery channel's
    # removal pauses at once (trigger). Paused for another reason, resuming checks access again.
    await console.post(f"/api/schedules/{s['id']}/pause", headers=BOB)
    sql(stack, "update app.channels set is_allowlisted = false where id = %s", (bugs,))
    try:
        refused = await console.post(f"/api/schedules/{s['id']}/resume", headers=BOB)
        assert refused.json()["error"]["code"] == "channel_access"
    finally:
        sql(stack, "update app.channels set is_allowlisted = true where id = %s", (bugs,))
    assert (await console.post(f"/api/schedules/{s['id']}/resume", headers=BOB)).json()["status"] == "active"


async def test_admins_reassign_a_schedule_whose_owner_left(console: httpx.AsyncClient, stack: Stack) -> None:
    s = (await create(console, BOB, body(stack))).json()
    sql(
        stack,
        "update app.schedules set status = 'paused', pause_reason = 'owner_removed' where id = %s",
        (s["id"],),
    )
    quinn = sql(stack, "select m.id from app.org_memberships m join app.users u on u.id = m.user_id "
                       "where u.email = 'quinn@acme.test'")[0][0]  # fmt: skip
    data = {"owner_membership_id": str(quinn)}
    assert (
        await console.post(f"/api/schedules/{s['id']}/reassign", json=data, headers=BOB)
    ).status_code == 403
    moved = await console.post(f"/api/schedules/{s['id']}/reassign", json=data, headers=ALICE)
    assert moved.status_code == 200, moved.text
    assert (moved.json()["owner"]["email"], moved.json()["status"]) == ("quinn@acme.test", "paused")
    assert [x["id"] for x in await listing(console, QUINN)] == [s["id"]]
    resumed = await console.post(f"/api/schedules/{s['id']}/resume", headers=QUINN)
    assert resumed.json()["status"] == "active"  # Quinn reads #bugs too


async def test_delete_and_preview(console: httpx.AsyncClient, stack: Stack) -> None:
    s = (await create(console, BOB, body(stack))).json()
    assert (await console.delete(f"/api/schedules/{s['id']}", headers=BOB)).status_code == 204
    assert await listing(console, BOB) == []
    assert (await console.delete(f"/api/schedules/{s['id']}", headers=BOB)).status_code == 404
    preview = await console.post(
        "/api/schedules/preview", json={"cron": "0 9 * * 1", "timezone": "UTC"}, headers=BOB
    )
    assert preview.json()["description"] == "Every Monday at 09:00"
    assert len(preview.json()["next_runs"]) == 5
    bad = await console.post(
        "/api/schedules/preview", json={"cron": "* * * * *", "timezone": "UTC"}, headers=BOB
    )
    assert bad.json()["error"]["code"] == "too_frequent"


async def test_options_list_what_the_member_can_use(console: httpx.AsyncClient, stack: Stack) -> None:
    bob = (await console.get("/api/schedules/options", headers=BOB)).json()
    names = {c["name"] for c in bob["channels"]}
    assert {"bugs", "general"} <= names
    assert "leads-private" not in names
    assert "random" not in names
    assert "old-project" not in names  # archived
    assert bob["has_slack_account"] is True
    alice = (await console.get("/api/schedules/options", headers=ALICE)).json()
    assert "leads-private" in {c["name"] for c in alice["channels"]}


async def test_another_org_sees_and_touches_nothing(console: httpx.AsyncClient, stack: Stack) -> None:
    s = (await create(console, BOB, body(stack))).json()
    assert await listing(console, DAVE) == []
    for method, path in (
        ("post", f"/api/schedules/{s['id']}/pause"),
        ("post", f"/api/schedules/{s['id']}/resume"),
        ("delete", f"/api/schedules/{s['id']}"),
        ("patch", f"/api/schedules/{s['id']}"),
    ):
        kwargs: dict[str, Any] = {"json": {"name": "x"}} if method == "patch" else {}
        assert (await getattr(console, method)(path, headers=DAVE, **kwargs)).status_code == 404
    # A channel of another org cannot be used either.
    globex = channel(stack, "general", org="org_globex")
    refused = await create(console, BOB, body(stack, channels=[globex]))
    assert refused.json()["error"]["code"] == "bad_channels"


async def test_a_delivery_channel_off_the_allowlist_pauses_and_starts_a_sync(
    console: httpx.AsyncClient, stack: Stack
) -> None:
    general = channel(stack, "general")
    s = (await create(console, BOB, body(stack, delivery="channel", delivery_channel_id=general))).json()
    started = len(stack.temporal.started)
    try:
        response = await console.patch(
            f"/api/slack/channels/{general}", json={"is_allowlisted": False}, headers=ALICE
        )
        assert response.json()["paused_schedules"] == 1
        assert (await listing(console, BOB))[0]["pause_reason"] == "access_lost"
        assert [w for w, _, _ in stack.temporal.started[started:]] == ["ScheduleSyncWorkflow"]
    finally:
        sql(stack, "update app.channels set is_allowlisted = true where id = %s", (general,))
    assert s["id"]

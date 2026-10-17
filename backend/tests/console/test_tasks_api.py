"""Console tasks: the list and one task's timeline (D-201). Admins see every task of their org,
members only their own, and nobody sees another org's."""

from typing import Any
from uuid import UUID

import httpx
import psycopg
from psycopg.types.json import Jsonb

from tests.console.conftest import SigningKey, auth
from tests.world import World

ALICE = ("user_alice", "org_acme")  # admin
BOB = ("user_bob", "org_acme")  # member, the requester of the world's task
DAVE = ("user_dave", "org_globex")


def _timeline(world: World) -> tuple[UUID, UUID]:
    """Bob's task gets a full timeline; Alice gets a task of her own."""
    acme = world.acme
    task = acme.ids["tasks"]
    with psycopg.connect(world.url) as conn:
        conn.execute(
            "update app.task_steps set status = 'completed', summary = 'Read #general', "
            "started_at = now() - interval '5 minutes', finished_at = now() - interval '4 minutes' "
            "where id = %s",
            (acme.ids["task_steps"],),
        )
        conn.execute(
            "insert into app.audit_log (org_id, actor_type, actor_id, task_id, action, target, decision, reason, "
            "metadata, occurred_at) values (%s, 'agent', %s, %s, 'policy.tool', 'jira.create_issue', 'info', "
            "'Ask mode: reversible writes need approval.', %s, now() - interval '3 minutes')",
            (acme.id, acme.user, task, Jsonb({"outcome": "needs_approval", "code": "approval_required"})),
        )
        conn.execute(
            "insert into app.audit_log (org_id, actor_type, actor_id, task_id, action, target, decision, reason, "
            "metadata, occurred_at) values (%s, 'agent', %s, %s, 'policy.tool', 'jira.search_issues', 'allowed', "
            "'Read access.', %s, now() - interval '4 minutes')",
            (acme.id, acme.user, task, Jsonb({"outcome": "allowed", "code": "ok", "tier": "read"})),
        )
        conn.execute(
            "insert into app.external_actions (org_id, task_id, integration_key, operation, target_ref, status, "
            "idempotency_key) values (%s, %s, 'slack', 'post_status', 'C0X:1700000000.1', 'succeeded', %s)",
            (acme.id, task, f"task:{task}:status"),
        )
        conn.execute(
            "update app.approvals set status = 'approved', decided_by = %s, created_at = now() - interval "
            "'3 minutes', decided_at = now() - interval '2 minutes' where id = %s",
            (acme.admin, acme.ids["approvals"]),
        )
        conn.execute(
            "update app.external_actions set status = 'succeeded', target_ref = 'jira:KAN-7', "
            "performed_at = now() - interval '1 minute' where id = %s",
            (acme.ids["external_actions"],),
        )
        conn.execute(
            "update app.usage_events set purpose = 'classify', occurred_at = now() - interval '4 minutes' "
            "where task_id = %s",
            (task,),
        )
        conn.execute(
            "update app.tasks set created_at = now() - interval '6 minutes', started_at = now() - interval "
            "'6 minutes', finished_at = now(), tokens_used = 150 where id = %s",
            (task,),
        )
        conn.execute(
            "update app.connections set status = 'active', external_account_label = 'https://acme.atlassian.net' "
            "where id = %s",
            (acme.ids["connections"],),
        )
        row = conn.execute(
            "insert into app.tasks (org_id, session_id, requested_by, trigger, model_tier, status) "
            "values (%s, %s, %s, 'mention', 'standard', 'running') returning id",
            (acme.id, acme.ids["sessions"], acme.admin),
        ).fetchone()
    assert row is not None
    return task, row[0]


def _ids(body: dict[str, Any]) -> set[str]:
    return {t["id"] for t in body["items"]}


async def test_admins_see_every_task_members_their_own(
    console: httpx.AsyncClient, signing_key: SigningKey, module_world: World
) -> None:
    bobs, alices = _timeline(module_world)
    alice, bob = auth(signing_key, *ALICE), auth(signing_key, *BOB)

    everyone = (await console.get("/api/tasks", headers=alice)).json()
    assert {str(bobs), str(alices)} <= _ids(everyone)
    own = (await console.get("/api/tasks", headers=bob)).json()
    assert _ids(own) == {str(bobs)}
    listed = own["items"][0]
    assert listed["title"] == "acme goal"  # the brief's goal, never message text
    assert listed["requester"] == "bob@acme.test"
    assert listed["error"] is None

    active = (await console.get("/api/tasks?status=active", headers=alice)).json()
    assert str(alices) in _ids(active)
    assert str(bobs) not in _ids(active)
    assert (await console.get("/api/tasks?status=sideways", headers=alice)).status_code == 422

    # A member cannot open someone else's task; nobody opens another org's.
    assert (await console.get(f"/api/tasks/{alices}", headers=bob)).status_code == 404
    dave = auth(signing_key, *DAVE)
    assert (await console.get(f"/api/tasks/{bobs}", headers=dave)).status_code == 404
    assert str(bobs) not in _ids((await console.get("/api/tasks", headers=dave)).json())


async def test_a_task_timeline_shows_what_the_agent_did(
    console: httpx.AsyncClient, signing_key: SigningKey, module_world: World
) -> None:
    task = module_world.acme.ids["tasks"]
    detail = (await console.get(f"/api/tasks/{task}", headers=auth(signing_key, *BOB))).json()

    assert detail["task"]["title"] == "acme goal"
    assert detail["usage"] == {"calls": 1, "input_tokens": 100, "output_tokens": 50}
    assert {
        "kind": "jira_issue",
        "label": "KAN-7",
        "url": "https://acme.atlassian.net/browse/KAN-7",
    } in detail["links"]
    assert {"kind": "pull_request", "label": "acme/api#1", "url": None} in detail["links"]
    [approval] = detail["approvals"]
    assert approval["status"] == "approved"
    assert approval["decided_by"] == "Alice Admin"  # the name, not the email (demo rehearsal)
    assert approval["decides"] == "the requester or an admin"

    events = detail["events"]
    kinds = [e["kind"] for e in events]
    assert {"step", "policy", "approval", "action", "model", "artifact"} <= set(kinds)
    times = [e["at"] for e in events]
    assert times == sorted(times)
    read = next(e for e in events if e["kind"] == "policy" and e["read"])
    assert read["target"] == "jira.search_issues"
    policy = next(e for e in events if e["kind"] == "policy" and not e["read"])
    assert policy["tone"] == "amber"
    assert policy["title"] == "Policy: jira.create_issue"
    model = next(e for e in events if e["kind"] == "model")
    assert model["title"] == "Model call: classify"
    assert model["input_tokens"] == 100
    assert any(e["title"] == "Approved by Alice Admin" for e in events)
    [action] = [e for e in events if e["kind"] == "action"]  # the task's own status message is not listed
    assert action["title"] == "Created a Jira issue"
    assert action["detail"] == "KAN-7"
    assert action["tone"] == "green"


def test_a_recheck_after_an_approval_reads_as_approved() -> None:
    """The push's approval covers the ticket moves that ride on it (D-198): their rechecks are not
    "needs approval" in the timeline; a write that gets its own approval request still is."""
    from datetime import UTC, datetime, timedelta

    from app.api.console.tasks import _events

    t0 = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)

    def at(seconds: int) -> datetime:
        return t0 + timedelta(seconds=seconds)

    def check(seconds: int, target: str) -> dict[str, Any]:
        return {"at": at(seconds), "action": "policy.tool", "target": target, "decision": "info",
                "reason": "Ask mode: reversible writes need approval.", "outcome": "needs_approval",
                "code": "approval_required", "tier": "reversible_write"}  # fmt: skip

    decisions = [
        check(10, "github.push_changes"),
        check(30, "jira.transition_issue"),
        check(40, "slack.post_message"),
    ]
    approvals = [
        {"created_at": at(11), "action_summary": "Push", "approver_rule": "requester_or_admin", "approver_role": None,
         "status": "approved", "decided_at": at(20), "decided_by": "alice@acme.test"},
        {"created_at": at(41), "action_summary": "Post", "approver_rule": "requester_or_admin", "approver_role": None,
         "status": "pending", "decided_at": None, "decided_by": None},
    ]  # fmt: skip
    actions = [
        {"integration_key": "jira", "operation": "transition_issue", "target_ref": "jira:KAN-15",
         "status": "succeeded", "created_at": at(31), "performed_at": at(31)},
        {"integration_key": "slack", "operation": "post_message", "target_ref": "C1:1.2",
         "status": "succeeded", "created_at": at(60), "performed_at": at(60)},
    ]  # fmt: skip
    events = {
        e["target"]: e for e in _events([], decisions, approvals, actions, [], []) if e["kind"] == "policy"
    }
    assert events["jira.transition_issue"]["tone"] == "green"
    assert events["jira.transition_issue"]["detail"] == "Done under the approval given above."
    assert events["github.push_changes"]["tone"] == "amber"  # asked for its own approval
    assert events["slack.post_message"]["tone"] == "amber"  # a new approval request came first

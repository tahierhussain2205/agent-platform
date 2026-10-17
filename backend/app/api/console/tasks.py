"""Console: the agent's tasks, and for one task a timeline of what it did (D-201).

Everything here is read from what tasks already record: steps, policy decisions, approvals,
external writes, model calls and derived artifacts. No message text is stored (invariant 7), so a
task's title is its brief's goal. Admins see every task of the org; members see their own."""

import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.console.common import fetch_all, fetch_one
from app.api.errors import ApiError
from app.auth.console import ConsoleIdentity, console_identity
from app.db.uow import UnitOfWork, get_uow
from app.settings import get_settings

router = APIRouter(prefix="/api/tasks")

ACTIVE = ("queued", "running", "awaiting_approval")
StatusFilter = Literal["active", "queued", "running", "awaiting_approval", "completed", "failed", "cancelled"]
MAX_EVENTS = 300
# The task's own status message, edited as it runs: not something it did.
QUIET_WRITES = {"post_status"}
WRITE_TITLES = {
    "slack.post_message": "Posted in Slack",
    "slack.post_approval": "Asked for approval in Slack",
    "jira.create_issue": "Created a Jira issue",
    "jira.add_comment": "Commented on a Jira issue",
    "jira.transition_issue": "Moved a Jira issue",
    "jira.assign_issue": "Assigned a Jira issue",
    "github.push_changes": "Pushed a branch and opened a pull request",
}
SLACK_MESSAGE = re.compile(r"^[CDG][A-Z0-9]+:\d+\.\d+$")  # a channel and ts: says nothing to a reader

TASK_SQL = """
select t.id, t.status, t.trigger, t.effective_mode::text as mode, t.model_tier::text as tier,
       t.tokens_used, t.cost, t.created_at, t.started_at, t.finished_at, t.error, t.requested_by,
       p.key as playbook, p.title as playbook_title, u.email as requester, u.name as requester_name,
       c.name as channel, s.external_channel_id, s.external_thread_id, w.external_id as team_id,
       coalesce(
         (select a.content->>'goal' from app.artifacts a
           where a.task_id = t.id and a.kind = 'brief' and a.content is not null
           order by a.created_at desc limit 1),
         sc.name) as title,
       (select count(*) from app.approvals ap where ap.task_id = t.id and ap.status = 'pending'
           and ap.expires_at > now()) as pending_approvals
  from app.tasks t
  left join app.playbooks p        on p.id = t.playbook_id
  left join app.org_memberships m  on m.id = t.requested_by
  left join app.users u            on u.id = m.user_id
  left join app.sessions s         on s.id = t.session_id
  left join app.channels c         on c.id = s.channel_id
  left join app.workspaces w       on w.id = s.workspace_id
  left join app.schedules sc       on sc.id = t.schedule_id
 where {where}
"""


def _task_json(row: dict[str, Any], identity: ConsoleIdentity) -> dict[str, Any]:
    out = {
        k: row[k]
        for k in (
            "id", "status", "trigger", "mode", "tier", "tokens_used", "created_at", "started_at",
            "finished_at", "playbook", "playbook_title", "requester", "requester_name", "channel", "title",
            "pending_approvals",
        )
    }  # fmt: skip
    out["cost"] = str(row["cost"]) if isinstance(row["cost"], Decimal) else row["cost"]
    out["dm"] = str(row.get("external_channel_id") or "").startswith("D")
    # Failure reasons are for admins: they can name internals a requester has no use for.
    out["error"] = ((row["error"] or "")[:300] or None) if identity.is_admin else None
    return out


def _visible(identity: ConsoleIdentity) -> tuple[str, dict[str, Any]]:
    if identity.is_admin:
        return "true", {}
    return "t.requested_by = %(me)s", {"me": identity.membership_id}


@router.get("")
async def list_tasks(
    identity: ConsoleIdentity = Depends(console_identity),
    uow: UnitOfWork = Depends(get_uow),
    status: StatusFilter | None = None,
    limit: int = Query(50, ge=1, le=200),
    before: datetime | None = None,
) -> dict[str, Any]:
    where, params = _visible(identity)
    clauses = [where]
    if status == "active":
        clauses.append("t.status = any(%(active)s)")
        params["active"] = list(ACTIVE)
    elif status:
        clauses.append("t.status = %(status)s")
        params["status"] = status
    if before is not None:
        clauses.append("t.created_at < %(before)s")
        params["before"] = before
    params["limit"] = limit + 1
    rows = await fetch_all(
        uow.connection,
        TASK_SQL.format(where=" and ".join(clauses))
        + " order by t.created_at desc, t.id desc limit %(limit)s",
        params,
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": [_task_json(r, identity) for r in rows],
        "next_before": rows[-1]["created_at"].isoformat() if more and rows else None,
    }


@router.get("/{task_id}")
async def get_task(
    task_id: UUID, identity: ConsoleIdentity = Depends(console_identity), uow: UnitOfWork = Depends(get_uow)
) -> dict[str, Any]:
    conn = uow.connection
    where, params = _visible(identity)
    row = await fetch_one(
        conn, TASK_SQL.format(where=f"t.id = %(id)s and {where}"), {**params, "id": task_id}
    )
    if row is None:  # another org's (row level security) or, for a member, someone else's
        raise ApiError(404, "not_found", "No such task.")
    since = {"id": task_id, "since": row["created_at"]}
    steps = await fetch_all(
        conn,
        "select kind, status, summary, started_at, finished_at from app.task_steps where task_id = %(id)s "
        "order by seq",
        since,
    )
    decisions = await fetch_all(
        conn,
        "select occurred_at as at, action, target, decision, reason, metadata->>'outcome' as outcome, "
        "metadata->>'code' as code, metadata->>'tier' as tier from app.audit_log "
        "where task_id = %(id)s and action like 'policy.%%' and occurred_at >= %(since)s "
        "order by occurred_at limit 300",
        since,
    )
    approvals = await fetch_all(
        conn,
        "select a.id, a.created_at, a.action_summary, a.action_tier, a.approver_rule, "
        "r.title as approver_role, a.status, a.decided_at, a.expires_at, u.email as decided_by "
        "from app.approvals a "
        "left join app.custom_roles r on r.id = a.approver_role_id "
        "left join app.org_memberships m on m.id = a.decided_by left join app.users u on u.id = m.user_id "
        "where a.task_id = %(id)s order by a.created_at",
        since,
    )
    actions = await fetch_all(
        conn,
        "select integration_key, operation, target_ref, status, created_at, performed_at "
        "from app.external_actions where task_id = %(id)s order by created_at",
        since,
    )
    calls = await fetch_all(
        conn,
        "select occurred_at as at, purpose, model, input_tokens, output_tokens, cached_tokens, cost "
        "from app.usage_events where task_id = %(id)s and kind = 'llm' and occurred_at >= %(since)s "
        "order by occurred_at limit 300",
        since,
    )
    artifacts = await fetch_all(
        conn,
        "select kind, created_at, content->>'goal' as goal, content->>'summary' as summary, "
        "content->>'delivered' as delivered from app.artifacts where task_id = %(id)s order by created_at",
        since,
    )
    links = await fetch_all(
        conn,
        "select kind, external_id, url, created_at from app.task_links where task_id = %(id)s "
        "order by created_at",
        since,
    )
    site = await fetch_one(
        conn,
        "select external_account_label as site from app.connections where integration_key = 'jira' "
        "and scope = 'org' and status = 'active' order by created_at desc limit 1",
    )
    task = _task_json(row, identity)
    task["slack_url"] = _slack_url(row)
    return {
        "task": task,
        "steps": steps,
        "approvals": [{**a, "decides": _decides(a)} for a in approvals],
        "links": _links(links, actions, (site or {}).get("site")),
        "usage": {
            "calls": len(calls),
            "input_tokens": sum(int(c["input_tokens"]) for c in calls),
            "output_tokens": sum(int(c["output_tokens"]) for c in calls),
        },
        "events": _events(steps, decisions, approvals, actions, calls, artifacts),
    }


def _slack_url(row: dict[str, Any]) -> str | None:
    """The thread in Slack's web client (real Slack only: the local stand-in has its own UI)."""
    team, channel, thread = row.get("team_id"), row.get("external_channel_id"), row.get("external_thread_id")
    if not team or not channel or "slack.com" not in get_settings().slack_api_url:
        return None
    base = f"https://app.slack.com/client/{team}/{channel}"
    return base if not thread or thread == "dm" else f"{base}/thread/{channel}-{thread}"


def _decides(approval: dict[str, Any]) -> str:
    role = approval.get("approver_role")
    return {
        "requester": "the requester",
        "admin": "an admin",
        "requester_or_admin": "the requester or an admin",
        "role_or_admin": f"a {role} or an admin" if role else "an admin",
    }.get(str(approval.get("approver_rule")), "")


def _links(
    links: list[dict[str, Any]], actions: list[dict[str, Any]], jira_site: str | None
) -> list[dict[str, Any]]:
    """Pull requests the task opened and Jira issues it created, with a link where we have one."""
    out = [{"kind": link["kind"], "label": link["external_id"], "url": link["url"]} for link in links]
    seen = {o["label"] for o in out}
    site = jira_site.rstrip("/") if jira_site and jira_site.startswith("https://") else None
    for a in actions:
        target = str(a.get("target_ref") or "")
        if a["status"] != "succeeded" or a["integration_key"] != "jira" or not target.startswith("jira:"):
            continue
        key = target.removeprefix("jira:")
        if a["operation"] == "create_issue" and key not in seen:
            seen.add(key)
            out.append({"kind": "jira_issue", "label": key, "url": f"{site}/browse/{key}" if site else None})
    return out


Event = dict[str, Any]


def _events(
    steps: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
) -> list[Event]:
    """One time ordered list: what the task did, what the policy decided, what people decided."""
    events: list[Event] = []

    def add(
        at: Any, kind: str, title: str, detail: str | None = None, tone: str = "neutral", **extra: Any
    ) -> None:
        if at is not None:
            events.append({"at": at, "kind": kind, "title": title, "detail": detail, "tone": tone, **extra})

    for s in steps:
        add(s["started_at"], "step", f"{s['kind'].title()} started", None, "neutral", step=s["kind"])
        if s["finished_at"] is not None:
            tone = {"completed": "green", "failed": "red", "skipped": "neutral"}.get(s["status"], "neutral")
            add(
                s["finished_at"],
                "step",
                f"{s['kind'].title()} {s['status']}",
                s["summary"],
                tone,
                step=s["kind"],
            )
    for d in decisions:
        outcome = d["outcome"] or d["decision"]
        tone = {"allowed": "green", "denied": "red", "needs_approval": "amber"}.get(outcome, "neutral")
        label = "Output check" if d["action"] == "policy.output" else "Policy"
        read = d["tier"] == "read" and outcome == "allowed"  # the console folds runs of these into one line
        add(
            d["at"], "policy", f"{label}: {d['target']}", d["reason"], tone, outcome=outcome, code=d["code"],
            read=read, target=d["target"],
        )  # fmt: skip
    for a in approvals:
        add(
            a["created_at"], "approval", "Approval requested", a["action_summary"][:300], "amber",
            decides=_decides(a), status=a["status"],
        )  # fmt: skip
        if a["decided_at"] is not None:
            approved = a["status"] == "approved"
            who = a["decided_by"] or "someone"
            add(
                a["decided_at"], "approval", f"{'Approved' if approved else 'Rejected'} by {who}",
                a["action_summary"][:300], "green" if approved else "red", status=a["status"],
            )  # fmt: skip
    for x in actions:
        if x["operation"] in QUIET_WRITES:
            continue
        tone = {"succeeded": "green", "failed": "red"}.get(x["status"], "neutral")
        target = x["target_ref"] or ""
        name = f"{x['integration_key']}.{x['operation']}"
        add(
            x["performed_at"] or x["created_at"], "action", WRITE_TITLES.get(name, name),
            None if SLACK_MESSAGE.match(target) else target.removeprefix("jira:") or None, tone,
            status=x["status"], operation=name,
        )  # fmt: skip
    for c in calls:
        add(
            c["at"], "model", f"Model call: {c['purpose'] or 'step'}", c["model"], "neutral",
            input_tokens=int(c["input_tokens"]), output_tokens=int(c["output_tokens"]),
        )  # fmt: skip
    for art in artifacts:
        text = (
            art["goal"] or art["summary"] or (f"delivered: {art['delivered']}" if art["delivered"] else None)
        )
        add(art["created_at"], "artifact", f"Saved the {art['kind']}", (text or "")[:300] or None)
    events.sort(key=lambda e: e["at"])
    _covered(events)
    return events[:MAX_EVENTS]


def _covered(events: list[Event]) -> None:
    """A write the policy rechecks when it runs, after a person approved it (or approved the push
    it rides on, D-198), reads "needs approval" right before it is done: say it was approved."""
    for i, event in enumerate(events):
        if event["kind"] != "policy" or event.get("outcome") != "needs_approval":
            continue
        name = str(event.get("target") or "").split(" ", 1)[0]
        for later in events[i + 1 :]:
            if later["kind"] == "approval" and later.get("status") and later["title"] == "Approval requested":
                break  # its own approval comes next
            if later["kind"] == "action" and later.get("operation") == name:
                if later.get("status") == "succeeded" and any(
                    e["kind"] == "approval" and e["title"].startswith("Approved by") for e in events[:i]
                ):
                    event["tone"] = "green"
                    event["detail"] = "Done under the approval given above."
                break

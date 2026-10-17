"""Checks on proposed Jira writes before they reach the policy (spec 3.2; plan phase 10).

The model proposes tickets; code decides what is sent:

- a ticket whose summary matches an open issue (searched now, within the requester's access) or an
  earlier ticket of the same plan is dropped as a duplicate of it;
- an assignee needs evidence: a candidate of the service catalog for the ticket's text or component,
  or a person the requester named. Otherwise the catalog's clear best match, or nobody (and the
  ticket says so). The reasons travel with the action, so approvers see why;
- a missing project or component comes from the matched service, and a component the project does
  not have is left out (team-managed projects have none), so Jira does not refuse the ticket;
- the description links back to the Slack messages of the finding the ticket comes from;
- a ticket built from a private conversation always needs approval, with a warning.
"""

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

from app.agent.types import PlannedAction
from app.db.tenant import tenant_tx
from app.policy.redaction import redact
from app.slack.adapter import escape
from app.tools.base import tool_name
from app.tools.catalog import words
from app.tools.executor import ToolExecutor

log = logging.getLogger(__name__)
MAX_LINKS = 5
PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
Permalinks = Callable[[list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]]


def overlap(a: str, b: str) -> float:
    """How much two summaries are about the same thing: the share of the shorter one's words that are
    in the other (0 when fewer than two words are shared, unless both are one same word)."""
    wa, wb = words(a), words(b)
    shared = wa & wb
    if not shared:
        return 0.0
    if min(len(wa), len(wb)) < 2:
        return 1.0 if wa == wb else 0.0
    return len(shared) / min(len(wa), len(wb)) if len(shared) >= 2 else 0.0


def similar(a: str, b: str) -> bool:
    """Two summaries about the same problem: most of the shorter one's words are in the other."""
    return overlap(a, b) >= 0.6


def closest(summary: str, issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The open issue most like a summary, if one is similar enough (the oldest of equals)."""
    scored = [(overlap(summary, str(i.get("summary") or "")), _number(i), i) for i in issues]
    best = max(
        ((score, -n, i) for score, n, i in scored if score >= 0.6), key=lambda t: (t[0], t[1]), default=None
    )
    return best[2] if best else None


def _number(issue: dict[str, Any]) -> int:
    tail = str(issue.get("key") or "").rpartition("-")[2]
    return int(tail) if tail.isdigit() else 0


def finding_for(raw: Any, summary: str, points: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The finding a ticket cites (1 based), or the one its summary shares most words with."""
    try:
        index = int(raw) - 1
    except (TypeError, ValueError):
        index = -1
    if 0 <= index < len(points):
        return points[index]
    scored = [(len(words(p["text"]) & words(summary)), i) for i, p in enumerate(points)]
    best = max(scored, default=(0, -1))
    return points[best[1]] if best[0] >= 2 else None


async def open_issues(executor: ToolExecutor, project: str, summary: str) -> tuple[list[dict[str, Any]], str]:
    """Open issues of a project that could be the same problem: the recently updated ones and those
    mentioning the summary's longest words. Only what the requester may browse."""
    queries = [f"project = {project} AND statusCategory != Done"]
    longest = sorted(words(summary), key=lambda w: (-len(w), w))[:2]
    if longest:
        queries.append(f'project = {project} AND statusCategory != Done AND text ~ "{" ".join(longest)}"')
    found: dict[str, dict[str, Any]] = {}
    for jql in queries:
        result = await executor.run(tool_name("jira", "search_issues"), {"jql": jql, "limit": 25})
        if result.is_error or result.denied:
            return [], result.text
        for issue in result.data.get("issues") or []:
            if not issue.get("done"):
                found.setdefault(str(issue["key"]), issue)
    return list(found.values()), ""


async def duplicate_of(executor: ToolExecutor, project: str, summary: str) -> dict[str, Any] | None:
    issues, _ = await open_issues(executor, project, summary)
    return closest(summary, issues)


def issue_link(issue: dict[str, Any]) -> str:
    return f"<{issue['url']}|{issue['key']}>" if issue.get("url") else str(issue["key"])


async def _member(executor: ToolExecutor, email: str) -> str | None:
    """The name of the active member with this email, if there is one."""
    async with tenant_tx(executor.ctx.pool, executor.ctx.org_id) as conn:
        row = await (
            await conn.execute(
                "select coalesce(u.name, u.email) from app.org_memberships m join app.users u on u.id = "
                "m.user_id where m.status = 'active' and lower(u.email) = %s",
                (email.lower(),),
            )
        ).fetchone()
    return str(row[0]) if row else None


def named_in(goal: str, email: str, name: str | None) -> bool:
    """Whether the request names this person (their email, full name or first name)."""
    lowered = goal.lower()
    if email.lower() in lowered:
        return True
    if not name:
        return False
    full = name.lower()
    first = full.split()[0] if full.split() else ""
    return any(n and re.search(rf"(?<![\w@]){re.escape(n)}(?![\w@])", lowered) for n in (full, first))


@dataclass
class Choice:
    email: str = ""  # "" leaves the ticket unassigned
    name: str = ""
    why: str = ""  # the reasons, or why nobody was assigned
    notes: list[str] = field(default_factory=list)

    @property
    def evidence(self) -> list[str]:
        line = (
            f"Assignee: {self.name} ({self.email}), {self.why}" if self.email else f"Unassigned: {self.why}"
        )
        return [*self.notes, line]

    @property
    def for_ticket(self) -> str:
        return (
            f"Assigned by the agent to {self.name}: {self.why}." if self.email else f"Unassigned: {self.why}."
        )


async def choose_assignee(
    executor: ToolExecutor, wanted: str, candidates: list[dict[str, Any]], goal: str
) -> Choice:
    by_email = {str(c["email"]).lower(): c for c in candidates}
    wanted = wanted.strip().lower()
    if wanted in by_email:
        c = by_email[wanted]
        return Choice(wanted, str(c["name"]), "; ".join(c["reasons"]))
    if wanted:
        name = await _member(executor, wanted)
        requester = (executor.ctx.requester_email or "").lower()
        if named_in(goal, wanted, name) and wanted != requester:  # who asks is in every request
            return Choice(wanted, name or wanted, "named in the request")
    notes = [f"Not assigned to {wanted}: the service catalog has no evidence they own this"] if wanted else []
    if candidates and (len(candidates) == 1 or candidates[0]["score"] > candidates[1]["score"]):
        c = candidates[0]
        return Choice(str(c["email"]).lower(), str(c["name"]), "; ".join(c["reasons"]), notes)
    if candidates:
        listed = ", ".join(f"{c['name']} ({'; '.join(c['reasons'])})" for c in candidates[:3])
        return Choice(why=f"the service catalog suggests more than one owner: {listed}", notes=notes)
    return Choice(why="no owner in the service catalog matches", notes=notes)


async def review(
    executor: ToolExecutor,
    actions: list[PlannedAction],
    points: list[dict[str, Any]],
    goal: str,
    permalinks: Permalinks,
    repositories: list[str] | None = None,
) -> list[PlannedAction]:
    """The actions as they will be offered to the policy; dropped ones carry ``skip``."""
    out: list[PlannedAction] = []
    planned: list[tuple[str, str]] = []  # (project, summary) of the tickets kept so far
    for action in actions:
        if action.tool != "jira":
            out.append(action)
            continue
        args = dict(action.args)
        if action.operation == "add_comment":
            args["text"] = redact(str(args.get("text") or ""))[0][:5000]
            out.append(replace(action, args=args))
            continue
        if action.operation == "assign_issue":
            choice = await choose_assignee(executor, str(args.get("assignee_email") or ""), [], goal)
            if not choice.email:
                reason = choice.evidence[0].removeprefix("Not assigned to ")
                key = escape(str(args.get("key")))
                out.append(replace(action, skip=f"Not assigning {key} to {escape(reason)}"))
                continue
            out.append(
                replace(action, args={**args, "assignee_email": choice.email}, evidence=choice.evidence)
            )
            continue
        if action.operation != "create_issue":
            out.append(action)
            continue
        out.append(
            await _ticket(executor, action, args, points, goal, permalinks, planned, repositories or [])
        )
    return out


async def _ticket(
    executor: ToolExecutor,
    action: PlannedAction,
    args: dict[str, Any],
    points: list[dict[str, Any]],
    goal: str,
    permalinks: Permalinks,
    planned: list[tuple[str, str]],
    repositories: list[str],
) -> PlannedAction:
    summary = redact(" ".join(without_refs(str(args.get("summary") or "")).split()))[0][:255]
    description = redact(without_refs(str(args.get("description") or "")).strip())[0][:20000]
    project = str(args.get("project") or "").strip().upper()
    component = str(args.get("component") or "").strip()
    finding = finding_for(args.pop("finding", None), summary, points)
    shown = escape(summary)  # for Slack
    if not summary:
        return replace(action, skip="A proposed ticket had no summary, so it was not created.")
    match: dict[str, Any] = {
        "text": f"{summary}\n{description}"[:2000],
        "project": project,
        "component": component,
    }
    if repositories:  # named in the request: the service is the one of that repository
        match["repository"] = repositories[0]
    owners = await executor.run(tool_name("catalog", "find_owners"), match)
    services = owners.data.get("services") or []
    best = services[0] if services else {}
    if not project and best.get("jira_project"):
        project = str(best["jira_project"])
    if not component and best.get("jira_component") and best.get("jira_project") == project:
        component = str(best["jira_component"])
    if not PROJECT_RE.match(project):
        project, choices = await _only_project(executor)
        if not project:
            named = f" ({choices})" if choices else ""
            return replace(action, skip=f'No Jira project for "{shown}": name one{named} and ask again.')
    earlier = next((s for p, s in planned if p == project and similar(summary, s)), None)
    if earlier:
        return replace(
            action, skip=f'"{shown}" is the same problem as "{escape(earlier)}": one ticket is enough.'
        )
    issues, failed = await open_issues(executor, project, summary)
    same = closest(summary, issues)
    if same is not None:
        return replace(
            action,
            skip=f'Not created: "{shown}" looks like {issue_link(same)} ({escape(str(same.get("status")))}), '
            "already open.",
        )
    planned.append((project, summary))
    evidence: list[str] = []
    if component:
        component, missing = await _component_of(executor, project, component)
        if missing and missing_was_asked(missing, goal):  # one the model made up is just left out
            evidence.append(missing)
    ask = action.ask
    if failed:
        evidence.append(f"⚠️ I couldn't check {project} for duplicates ({failed[:120]})")
        ask = True
    choice = await choose_assignee(
        executor, str(args.get("assignee_email") or ""), owners.data.get("candidates") or [], goal
    )
    evidence = [*choice.evidence, *evidence]
    sources = [s for s in (finding or {}).get("sources") or [] if s.get("kind", "slack") == "slack"]
    private = sorted(
        {f"#{s.get('channel_name')}" for s in sources if s.get("visibility") in ("private", "dm")}
    )
    if private:
        evidence.append(
            f"⚠️ From a private conversation ({', '.join(private)}): everyone who can browse {project} "
            "will see this ticket"
        )
        ask = True
    links = [s["permalink"] for s in await permalinks(sources[:MAX_LINKS]) if s.get("permalink")]
    parts = [description] if description else []
    if links:
        parts.append("Reported in Slack:\n" + "\n".join(f"• {link}" for link in links))
    parts.append(choice.for_ticket)
    title = f'Create {project} ticket "{shown}"' + (
        f" for {escape(choice.name)}" if choice.email else " (unassigned)"
    )
    return replace(
        action,
        args={
            **args,
            "project": project,
            "summary": summary,
            "description": "\n\n".join(parts),
            "assignee_email": choice.email,
            "component": component,
        },
        summary=title[:300],
        evidence=evidence,
        ask=ask,
    )


async def _only_project(executor: ToolExecutor) -> tuple[str, str]:
    """The one Jira project the requester can browse, or nothing and the ones there are."""
    listed = await executor.run(tool_name("jira", "list_projects"), {})
    keys = [
        str(p.get("key")) for p in listed.data.get("projects") or [] if PROJECT_RE.match(str(p.get("key")))
    ]
    if listed.is_error or not keys:
        return "", ""
    return (keys[0], "") if len(keys) == 1 else ("", ", ".join(keys[:8]))


def missing_was_asked(missing: str, goal: str) -> bool:
    """Whether the request asked for the component the project lacks (``missing`` names it): its name
    and the word "component" (a service's or repository's name alone is not a component)."""
    named = re.search(r"has no component (.+?), so", missing)
    lowered = goal.lower()
    return named is not None and "component" in lowered and named.group(1).lower() in lowered


REF = re.compile(r"\s+\[(?:[A-Z]{1,2})?\d{1,3}\](?=[\s.,;:)]|$)")  # after a space: not code like a[1]
# The model sees the findings numbered and sometimes cites them ("Finding 1.", "(finding 2)").
FINDING = re.compile(r"\s*\([Ff]inding #?\d{1,2}\)|\s*\b[Ff]inding #?\d{1,2}\b\.?(?=\s|$)")


def without_refs(text: str) -> str:
    """Model text without our citation refs ([S1], [C2], [3]) and finding numbers: they mean nothing
    in Jira."""
    return FINDING.sub("", REF.sub("", text))


async def _component_of(executor: ToolExecutor, project: str, component: str) -> tuple[str, str]:
    """The project's own name for ``component``, or nothing and why. A list that cannot be read keeps
    the component: Jira then says what is wrong."""
    listed = await executor.run(tool_name("jira", "list_components"), {"project": project})
    if listed.is_error:
        return component, ""
    names = {str(c.get("name")).lower(): str(c.get("name")) for c in listed.data.get("components") or []}
    if component.lower() in names:
        return names[component.lower()], ""
    return "", f"{project} has no component {component}, so the ticket has none"


def preview(action: PlannedAction, args: dict[str, Any]) -> str:
    """What approvers see for a Jira write: the ticket, the assignee and why."""
    lines = [action.summary]
    if action.operation == "create_issue":
        details = [f"Project {args.get('project')}"]
        if args.get("component"):
            details.append(f"component {args['component']}")
        lines.append(", ".join(details))
    lines += [escape(e) for e in action.evidence]
    text = str(args.get("description") or args.get("text") or "")
    if text:
        quoted = [f"> {escape(line)}" for line in text[:600].splitlines()]
        lines += quoted + (["> …"] if len(text) > 600 else [])
    return "\n".join(lines)

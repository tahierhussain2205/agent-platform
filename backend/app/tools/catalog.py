"""The service catalog as a tool: who owns what, and why (spec 3.2 step 3).

``find_owners`` ranks the confirmed services that match a text, repository, path or Jira component
and returns their owners with the reasons they are owners (a CODEOWNERS rule, a component lead, an
admin's choice). Proposed services do not count until an admin confirms them. It reads our own
database (row level security keeps it to the org), so it needs no connection.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from app.db.tenant import tenant_tx
from app.tools.base import Operation, Source, Target, ToolContext, ToolResult, cap

STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "when",
    "from",
    "that",
    "this",
    "are",
    "was",
    "service",
    "services",
    "fails",
    "fail",
    "error",
    "errors",
    "issue",
    "issues",
    "bug",
    "bugs",
    "again",
    "after",
    "over",
}
WORD = re.compile(r"[a-z0-9]+")

OPERATIONS = {
    "find_owners": Operation(
        "find_owners",
        "Who owns the code or service a problem is about, from the confirmed service catalog: give the "
        "problem's text, and a repository, path, Jira project or component when known. Returns owners with "
        "their emails and the reasons they own it.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string", "maxLength": 2000},
                "repository": {"type": "string", "description": "owner/name"},
                "path": {"type": "string"},
                "project": {"type": "string"},
                "component": {"type": "string"},
            },
        },
        credential_mode="org",
    ),
}


def words(text: str | None) -> set[str]:
    """Lower case words without stopwords, plural s dropped (carts and cart match)."""
    out = set()
    for w in WORD.findall((text or "").lower()):
        if len(w) < 3 or w in STOPWORDS:
            continue
        out.add(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return out


@dataclass
class Match:
    service_id: str
    name: str
    score: int
    why: list[str] = field(default_factory=list)
    jira_project: str | None = None
    jira_component: str | None = None


def rank(services: list[dict[str, Any]], args: dict[str, Any]) -> list[Match]:
    """Score services against what is known about a problem; best first, ties by name."""
    text = words(args.get("text"))
    repo = str(args.get("repository") or "").strip().lower()
    path = str(args.get("path") or "").strip().lstrip("/")
    project = str(args.get("project") or "").strip().upper()
    component = str(args.get("component") or "").strip().lower()
    matches = []
    for s in services:
        score, why = 0, []
        if component and (s.get("jira_component") or "").lower() == component:
            score += 5
            why.append(f"Jira component {s['jira_component']}")
        elif project and component == "" and s.get("jira_project") == project:
            score += 1
        if repo and (s.get("repository") or "").lower() == repo:
            score += 4
            why.append(f"repository {s['repository']}")
        if path and any(path.startswith(p.lstrip("/")) for p in s.get("paths") or []):
            score += 3
            why.append(f"path {path}")
        named = (
            words(s["name"])
            | words(s.get("jira_component"))
            | words((s.get("repository") or "").split("/")[-1])
        )
        about = words(s.get("description"))
        overlap = text & named
        if overlap:
            score += 2 * min(len(overlap), 2)
            why.append("mentions " + ", ".join(sorted(overlap)))
        elif text & about:
            score += 1
            why.append("matches its description")
        if score > 0:
            matches.append(
                Match(str(s["id"]), s["name"], score, why, s.get("jira_project"), s.get("jira_component"))
            )
    return sorted(matches, key=lambda m: (-m.score, m.name))


class CatalogAdapter:
    key = "catalog"
    operations = OPERATIONS

    async def resolve_target(self, ctx: ToolContext, operation: str, args: dict[str, Any]) -> Target | None:
        return None

    async def execute(
        self, ctx: ToolContext, operation: str, args: dict[str, Any], credential: Any = None
    ) -> ToolResult:
        if operation != "find_owners":
            return ToolResult(f"Unknown operation {operation}.", is_error=True)
        async with tenant_tx(ctx.pool, ctx.org_id) as conn:
            rows = await (
                await conn.execute(
                    "select s.id, s.name, s.description, s.jira_project, s.jira_component, s.paths, "
                    "r.full_name from app.services s left join app.repositories r on r.id = s.repository_id "
                    "where s.status = 'active'"
                )
            ).fetchall()
            columns = ("id", "name", "description", "jira_project", "jira_component", "paths", "repository")
            services = [dict(zip(columns, r, strict=True)) for r in rows]
            owners = await (
                await conn.execute(
                    "select so.service_id::text, u.email, coalesce(u.name, u.email), so.source, so.reason "
                    "from app.service_owners so join app.org_memberships m on m.id = so.membership_id "
                    "join app.users u on u.id = m.user_id join app.services s on s.id = so.service_id "
                    "where s.status = 'active' and m.status = 'active'"
                )
            ).fetchall()
        by_service: dict[str, list[tuple[str, str, str, str | None]]] = {}
        for service_id, email, name, source, reason in owners:
            by_service.setdefault(service_id, []).append((email, name, source, reason))
        matches = rank(services, args)[:3]
        if not matches:
            return ToolResult(
                "No confirmed service in the catalog matches.", data={"candidates": [], "services": []}
            )
        candidates: dict[str, dict[str, Any]] = {}
        matched: list[dict[str, Any]] = []
        lines = ["Owners from the service catalog (best match first):"]
        for match in matches:
            source = Source(
                ref=f"catalog:{match.service_id}",
                channel_external_id="",
                channel_name="Service catalog",
                visibility="public",
                ts="",
                label=f"Service {match.name}",
                kind="catalog",
            )
            short = ctx.refs.add(source)
            listed = []
            for email, name, origin, reason in by_service.get(match.service_id, []):
                because = f"owner of {match.name}" + (f" ({reason})" if reason else f" ({origin})")
                entry = candidates.setdefault(
                    email, {"email": email, "name": name, "score": 0, "reasons": []}
                )
                entry["score"] = max(entry["score"], match.score)
                entry["reasons"].append(because)
                listed.append(f"{name} <{email}>: {because}")
            jira = (
                f"Jira {match.jira_project}"
                + (f"/{match.jira_component}" if match.jira_component else "")
                + "; "
                if match.jira_project
                else ""
            )
            lines.append(
                f"[{short}] {match.name} ({jira}matched on {'; '.join(match.why)}): "
                + ("; ".join(listed) if listed else "no owners recorded")
            )
            matched.append(
                {
                    "name": match.name,
                    "score": match.score,
                    "jira_project": match.jira_project,
                    "jira_component": match.jira_component,
                }
            )
        ranked = sorted(candidates.values(), key=lambda c: (-c["score"], c["email"]))
        return ToolResult(cap("\n".join(lines)), data={"candidates": ranked, "services": matched})

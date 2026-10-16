"""Service catalog proposals (spec 3.2 step 3; plan phase 10).

From what the proposing admin may read: one service per repository the GitHub App sees, with
owners from CODEOWNERS; Jira components matched to those services (or proposed on their own) with
their leads. Owners become members through linked accounts (GitHub logins, Jira accounts) or, for
Jira, the member's email; anyone else stays in the evidence for an admin to resolve. Every read goes
through the ``ToolExecutor``, so the policy module and audit apply.

Saving never overwrites an admin's work: a confirmed service only gains missing links; an archived
name is not proposed again; a proposal is refreshed with its proposal owners replaced.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db.tenant import tenant_tx
from app.tools.base import tool_name
from app.tools.catalog import words
from app.tools.executor import ToolExecutor

log = logging.getLogger(__name__)
MAX_REPOS = 50


@dataclass
class Owner:
    membership_id: str
    source: str  # codeowners | component_lead
    reason: str


@dataclass
class Proposal:
    name: str
    description: str | None = None
    repository: dict[str, Any] | None = None  # id, full_name, default_branch
    paths: list[str] = field(default_factory=list)
    jira_project: str | None = None
    jira_component: str | None = None
    owners: list[Owner] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    segments: set[str] = field(default_factory=set)  # path segments of CODEOWNERS rules, for matching

    def add_owner(self, owner: Owner) -> None:
        """One owner per member; a second reason is kept next to the first."""
        known = next((o for o in self.owners if o.membership_id == owner.membership_id), None)
        if known is None:
            self.owners.append(owner)
        elif owner.reason not in known.reason:
            known.reason = f"{known.reason}; {owner.reason}"


def matches(component: dict[str, Any], proposal: Proposal) -> bool:
    """A Jira component belongs to a repository's service when they share a name word, the component's
    description names the repository, or the component is a directory in its CODEOWNERS."""
    if proposal.repository is None:
        return False
    short = proposal.repository["full_name"].split("/")[-1].lower()
    name = str(component.get("name") or "")
    if words(name) & words(short):
        return True
    if re.search(rf"(?<![\w-]){re.escape(short)}(?![\w-])", str(component.get("description") or "").lower()):
        return True
    return name.lower() in proposal.segments


def unique_name(name: str, project: str, proposals: list[Proposal]) -> str:
    """A component's service name, qualified by its project when another proposal has the name
    (Search in two projects would otherwise be one name twice, which the catalog refuses)."""
    clean = name.strip()
    taken = {p.name.strip().lower() for p in proposals}
    return f"{clean} ({project})" if clean.lower() in taken else clean


async def _owner_maps(
    executor: ToolExecutor, pool: Any, org_id: Any
) -> tuple[dict[str, str], dict[str, str]]:
    """GitHub logins and Jira account ids of active members: linked accounts first, then (for Jira)
    the account found for each member's email."""
    async with tenant_tx(pool, org_id) as conn:
        links = await (
            await conn.execute(
                "select c.integration_key, lower(c.external_account_id), c.membership_id::text "
                "from app.connections c join app.org_memberships m on m.id = c.membership_id "
                "where c.scope = 'user' and c.status = 'active' and m.status = 'active' "
                "and c.integration_key in ('github', 'jira') and c.external_account_id is not null"
            )
        ).fetchall()
        members = await (
            await conn.execute(
                "select lower(u.email), m.id::text from app.org_memberships m join app.users u on u.id = "
                "m.user_id "
                "where m.status = 'active'"
            )
        ).fetchall()
    github = {account: member for key, account, member in links if key == "github"}
    jira = {account: member for key, account, member in links if key == "jira"}
    if members and "jira" in executor.registry.adapters:
        by_email = dict(members)
        found = await executor.run(tool_name("jira", "match_accounts"), {"emails": list(by_email)})
        for email, account in (found.data.get("accounts") or {}).items():
            jira.setdefault(str(account).lower(), by_email[email])
    return github, jira


async def build(executor: ToolExecutor, pool: Any, org_id: Any) -> tuple[list[Proposal], list[str]]:
    """Proposals, and notes about what could not be read."""
    notes: list[str] = []
    github, jira_accounts = await _owner_maps(executor, pool, org_id)
    proposals: list[Proposal] = []
    if "github" in executor.registry.adapters:
        listed = await executor.run(tool_name("github", "list_repos"), {})
        if listed.is_error:
            notes.append(f"GitHub repositories: {listed.text}")
        for repo in (listed.data.get("repos") or [])[:MAX_REPOS]:
            full = repo["full_name"]
            p = Proposal(name=full.split("/")[-1], repository=repo)
            owners = await executor.run(tool_name("github", "get_codeowners"), {"repo": full})
            if owners.is_error:
                p.evidence.append(f"CODEOWNERS of {full} not read: {owners.text}")
            for rule in owners.data.get("rules") or []:
                pattern = rule["pattern"]
                if pattern not in ("*", "/") and pattern.rstrip("/*"):
                    p.paths.append(pattern)
                    p.segments |= {s.lower() for s in pattern.strip("/*").split("/") if s}
                for handle in rule["owners"]:
                    member = github.get(handle.lstrip("@").lower())
                    reason = f"CODEOWNERS {pattern} in {full}"
                    if member:
                        p.add_owner(Owner(member, "codeowners", reason))
                    else:
                        p.evidence.append(f"{reason}: {handle} (no member with this linked GitHub account)")
            proposals.append(p)
    if "jira" in executor.registry.adapters:
        projects = await executor.run(tool_name("jira", "list_projects"), {})
        if projects.is_error:
            notes.append(f"Jira projects: {projects.text}")
        for project in projects.data.get("projects") or []:
            key = project["key"]
            listed = await executor.run(tool_name("jira", "list_components"), {"project": key})
            if listed.is_error:
                notes.append(f"Jira components of {key}: {listed.text}")
            for component in listed.data.get("components") or []:
                target = next(
                    (p for p in proposals if p.jira_component is None and matches(component, p)), None
                )
                if target is None:
                    target = Proposal(
                        name=unique_name(str(component["name"]), key, proposals),
                        description=component.get("description") or None,
                    )
                    proposals.append(target)
                target.jira_project, target.jira_component = key, str(component["name"])
                lead = component.get("lead")
                reason = f"lead of Jira component {key}/{component['name']}"
                if lead and jira_accounts.get(str(lead).lower()):
                    target.add_owner(Owner(jira_accounts[str(lead).lower()], "component_lead", reason))
                elif lead:
                    target.evidence.append(f"{reason}: Jira account {lead} (no member matched)")
                else:
                    target.evidence.append(f"Jira component {key}/{component['name']} has no lead")
    return proposals, notes


async def save(pool: Any, org_id: Any, proposals: list[Proposal]) -> dict[str, int]:
    counts = {"proposed": 0, "refreshed": 0, "linked": 0, "kept": 0, "skipped": 0}
    async with tenant_tx(pool, org_id) as conn:
        for p in proposals:
            repository_id = None
            if p.repository is not None:
                row = await (
                    await conn.execute(
                        "insert into app.repositories (org_id, provider, external_id, full_name, "
                        "default_branch) "
                        "values (%s, 'github', %s, %s, %s) on conflict (org_id, provider, external_id) "
                        "do update "
                        "set full_name = excluded.full_name, default_branch = excluded.default_branch "
                        "returning id",
                        (
                            org_id,
                            p.repository["id"],
                            p.repository["full_name"],
                            p.repository["default_branch"],
                        ),
                    )
                ).fetchone()
                repository_id = row[0] if row else None
            existing = await (
                await conn.execute(
                    "select id, status from app.services where lower(btrim(name)) = lower(btrim(%s)) "
                    "order by status = 'archived'",
                    (p.name,),
                )
            ).fetchall()
            live = next((r for r in existing if r[1] != "archived"), None)
            if live is None and existing:  # an admin archived it: do not propose it again
                counts["skipped"] += 1
                continue
            if live is not None and live[1] == "active":
                updated = await (
                    await conn.execute(
                        "update app.services set repository_id = coalesce(repository_id, %s), "
                        "jira_project = coalesce(jira_project, %s), jira_component = "
                        "coalesce(jira_component, %s), "
                        "paths = case when paths = '{}' then %s else paths end where id = %s and ("
                        "(repository_id is null and %s::uuid is not null) or (jira_component is null and "
                        "%s::text is not null) "
                        "or (paths = '{}' and %s::text[] <> '{}')) returning id",
                        (
                            repository_id,
                            p.jira_project,
                            p.jira_component,
                            p.paths,
                            live[0],
                            repository_id,
                            p.jira_component,
                            p.paths,
                        ),
                    )
                ).fetchone()
                counts["linked" if updated else "kept"] += 1
                continue
            values = (
                p.description,
                repository_id,
                p.jira_project,
                p.jira_component,
                p.paths,
                Jsonb(p.evidence),
            )
            if live is None:
                row = await (
                    await conn.execute(
                        "insert into app.services (org_id, name, description, repository_id, jira_project, "
                        "jira_component, paths, evidence, status) values (%s, %s, %s, %s, %s, %s, %s, %s, "
                        "'proposed') returning id",
                        (org_id, p.name, *values),
                    )
                ).fetchone()
                assert row is not None
                service_id: UUID = row[0]
                counts["proposed"] += 1
            else:
                service_id = live[0]
                await conn.execute(
                    "update app.services set description = coalesce(%s, description), repository_id = %s, "
                    "jira_project = %s, jira_component = %s, paths = %s, evidence = %s where id = %s",
                    (*values, service_id),
                )
                await conn.execute(
                    "delete from app.service_owners where service_id = %s and source <> 'manual'",
                    (service_id,),
                )
                counts["refreshed"] += 1
            for owner in p.owners:
                await conn.execute(
                    "insert into app.service_owners (org_id, service_id, membership_id, source, reason) "
                    "values (%s, %s, %s, %s, %s) on conflict (service_id, membership_id) do nothing",
                    (org_id, service_id, owner.membership_id, owner.source, owner.reason[:500]),
                )
    return counts

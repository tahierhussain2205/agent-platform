"""Jira tool adapter: reads, and writes with the requester's own account.

With the org connection (credential mode ``org_with_check``, the default) every result is filtered
to projects the requester may browse: their Jira account is their linked one, or the account the
org connection finds for their email, and ``BROWSE_PROJECTS`` is checked per project. With a user
link (``user_required``, the default for writes), Jira itself applies the requester's permissions and
issues carry their name.

Writes are idempotent beyond ``external_actions``: a created issue carries a label derived from the
action's idempotency key and a comment a short reference, and a retry looks for them first.
"""

import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.policy import Member
from app.policy.redaction import redact
from app.tools.base import Operation, Source, Target, ToolContext, ToolResult, cap
from app.vault_service import Credential

log = logging.getLogger(__name__)
KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d{1,7}$")
PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
FIELDS = ["summary", "status", "assignee", "priority", "project", "updated", "description"]

OPERATIONS = {
    "search_issues": Operation(
        "search_issues",
        "Search Jira issues. Give a JQL query, or free text and optionally a project key. Each issue starts "
        "with a ref like [S2]; cite refs exactly.",
        {
            "type": "object",
            "properties": {
                "jql": {
                    "type": "string",
                    "description": 'A JQL query, for example project = PAY AND status = "To Do".',
                },
                "text": {"type": "string", "description": "Words to search for when no JQL is given."},
                "project": {"type": "string", "description": "A project key such as PAY."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
            },
        },
        credential_mode="org_with_check",
    ),
    "get_issue": Operation(
        "get_issue",
        "Read one Jira issue by key (for example PAY-101): summary, status, assignee, priority, description.",
        {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
        credential_mode="org_with_check",
    ),
    "list_projects": Operation(
        "list_projects",
        "List the Jira projects the requester can browse.",
        {"type": "object", "properties": {}},
        credential_mode="org_with_check",
    ),
    "report_issues": Operation(
        "report_issues",
        "The issues a progress report counts: every open issue of a project, and those updated since a time.",
        {
            "type": "object",
            "properties": {"project": {"type": "string"}, "since": {"type": "string"}},
            "required": ["project", "since"],
        },
        credential_mode="org_with_check",
        internal=True,
    ),
}
WRITE_SCHEMA_ISSUE = {
    "type": "object",
    "properties": {
        "project": {"type": "string", "description": "Project key, for example PAY."},
        "summary": {"type": "string", "maxLength": 255},
        "description": {"type": "string", "maxLength": 8000},
        "issue_type": {"type": "string", "enum": ["Bug", "Task", "Story"]},
        "assignee_email": {"type": "string", "description": "From catalog_find_owners; omit when unsure."},
        "component": {"type": "string"},
        "labels": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "finding": {"type": "integer", "description": "The number of the finding this ticket is for."},
    },
    "required": ["project", "summary"],
}
OPERATIONS.update(
    {
        "list_components": Operation(
            "list_components",
            "List the components of a Jira project, with their leads.",
            {"type": "object", "properties": {"project": {"type": "string"}}, "required": ["project"]},
            credential_mode="org_with_check",
        ),
        "match_accounts": Operation(
            "match_accounts",
            "Jira account ids for people's emails (to recognise members among component leads).",
            {
                "type": "object",
                "properties": {"emails": {"type": "array", "items": {"type": "string"}, "maxItems": 500}},
                "required": ["emails"],
            },
            credential_mode="org",
            internal=True,
        ),
        "create_issue": Operation(
            "create_issue",
            "Create a Jira issue with the requester's own Jira account (it carries their name).",
            WRITE_SCHEMA_ISSUE,
            kind="write",
            credential_mode="user_required",
        ),
        "assign_issue": Operation(
            "assign_issue",
            "Assign a Jira issue to someone, by their email.",
            {
                "type": "object",
                "properties": {"key": {"type": "string"}, "assignee_email": {"type": "string"}},
                "required": ["key", "assignee_email"],
            },
            kind="write",
            credential_mode="user_required",
        ),
        "add_comment": Operation(
            "add_comment",
            "Comment on a Jira issue with the requester's own Jira account.",
            {
                "type": "object",
                "properties": {"key": {"type": "string"}, "text": {"type": "string", "maxLength": 8000}},
                "required": ["key", "text"],
            },
            kind="write",
            credential_mode="user_required",
        ),
    }
)
LABEL_RE = re.compile(r"[^A-Za-z0-9_.-]+")
REPORT_FIELDS = [
    "summary",
    "status",
    "assignee",
    "priority",
    "project",
    "updated",
    "created",
    "resolutiondate",
]
MAX_REPORT_ISSUES = 1000


def adf_text(node: Any) -> str:
    """Plain text from Atlassian Document Format."""
    if isinstance(node, dict):
        if node.get("type") == "text":
            return str(node.get("text", ""))
        if node.get("type") == "hardBreak":
            return "\n"
        parts = [adf_text(c) for c in node.get("content") or []]
        sep = "\n" if node.get("type") in ("doc", "bulletList", "orderedList") else ""
        return sep.join(p for p in parts if p)
    if isinstance(node, list):
        return "\n".join(adf_text(n) for n in node)
    return str(node or "")


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class JiraAdapter:
    key = "jira"

    def __init__(self, api_url: str) -> None:
        self.api_url = api_url.rstrip("/")
        self.operations = OPERATIONS

    async def resolve_target(self, ctx: ToolContext, operation: str, args: dict[str, Any]) -> Target | None:
        return None

    # ------------------------------------------------------------------ HTTP

    def _base(self, credential: Credential) -> str:
        cloud = credential.details.get("cloud_id")
        if not cloud:
            raise ValueError("the Jira connection has no site")
        return f"{self.api_url}/ex/jira/{cloud}/rest/api/3"

    async def _call(self, ctx: ToolContext, credential: Credential, method: str, path: str, **kw: Any) -> Any:
        http: httpx.AsyncClient = ctx.http
        response = await http.request(
            method,
            f"{self._base(credential)}/{path}",
            headers={"authorization": f"Bearer {credential.token}", "accept": "application/json"},
            **kw,
        )
        if response.status_code == 404:
            return None
        if response.status_code == 204 or not response.content:
            return {}
        if response.status_code >= 400:
            try:
                messages = response.json().get("errorMessages") or [response.text[:200]]
            except ValueError:
                messages = [f"HTTP {response.status_code}"]
            raise JiraError("; ".join(str(m) for m in messages))
        return response.json()

    # ------------------------------------------------------------------ the requester's access

    async def _requester_account(self, ctx: ToolContext, credential: Credential) -> str | None:
        if "jira:me" in ctx.cache:
            return ctx.cache["jira:me"]  # type: ignore[no-any-return]
        from app.db.tenant import tenant_tx

        async with tenant_tx(ctx.pool, ctx.org_id) as conn:
            row = await (
                await conn.execute(
                    "select external_account_id from app.connections where integration_key = 'jira' "
                    "and scope = 'user' and membership_id = %s and status = 'active'",
                    (ctx.membership_id,),
                )
            ).fetchone()
        account = row[0] if row and row[0] else None
        if account is None and ctx.requester_email:
            account = await self._account_for_email(ctx, credential, ctx.requester_email)
        ctx.cache["jira:me"] = account
        return account

    async def _account_for_email(self, ctx: ToolContext, credential: Credential, email: str) -> str | None:
        found = await self._call(ctx, credential, "GET", "user/search", params={"query": email}) or []
        exact = [u for u in found if str(u.get("emailAddress", "")).lower() == email.lower()]
        return str(exact[0]["accountId"]) if exact else None

    async def _can_browse(self, ctx: ToolContext, credential: Credential, account: str, project: str) -> bool:
        key = f"jira:browse:{account}:{project}"
        if key not in ctx.cache:
            params = {"permissions": "BROWSE_PROJECTS", "projectKey": project, "accountId": account}
            users = await self._call(ctx, credential, "GET", "user/permission/search", params=params) or []
            ctx.cache[key] = any(u.get("accountId") == account for u in users)
        return bool(ctx.cache[key])

    async def _filter(
        self, ctx: ToolContext, credential: Credential, projects: list[str]
    ) -> tuple[set[str], str | None]:
        """Projects the requester may browse (all of them for a user link), and a refusal note."""
        if credential.scope == "user":
            return set(projects), None
        account = await self._requester_account(ctx, credential)
        if account is None:
            return set(), "I couldn't match you to a Jira account; link your Jira account in the console."
        return {p for p in set(projects) if await self._can_browse(ctx, credential, account, p)}, None

    async def audience_ok(
        self, ctx: ToolContext, credential: Credential, source: Source, members: list[Member]
    ) -> bool:
        """Every member has a Jira account (linked, or found by email) that may browse the project."""
        project = source.audience.split(":", 1)[1] if ":" in source.audience else ""
        if not PROJECT_RE.match(project):
            return False
        for member in members:
            account = member.links.get("jira") or await self._account_for_email(ctx, credential, member.email)
            if account is None or not await self._can_browse(ctx, credential, account, project):
                return False
        return True

    # ------------------------------------------------------------------ operations

    async def execute(
        self, ctx: ToolContext, operation: str, args: dict[str, Any], credential: Any = None
    ) -> ToolResult:
        if not isinstance(credential, Credential):
            return ToolResult("Jira is not connected.", is_error=True)
        try:
            if operation == "search_issues":
                return await self._search(ctx, credential, args)
            if operation == "get_issue":
                return await self._get(ctx, credential, str(args.get("key", "")).strip().upper())
            if operation == "list_projects":
                return await self._projects(ctx, credential)
            if operation == "report_issues":
                return await self._report_issues(ctx, credential, args)
            if operation == "list_components":
                return await self._components(ctx, credential, str(args.get("project", "")).strip().upper())
            if operation == "match_accounts":
                found = {}
                for email in [str(e).strip() for e in args.get("emails") or []][:500]:
                    account = await self._account_for_email(ctx, credential, email) if email else None
                    if account:
                        found[email.lower()] = account
                return ToolResult(f"{len(found)} of the emails have Jira accounts.", data={"accounts": found})
            if operation == "create_issue":
                return await self._create(ctx, credential, args)
            if operation == "assign_issue":
                return await self._assign(ctx, credential, args)
            if operation == "add_comment":
                return await self._comment(ctx, credential, args)
        except JiraError as exc:
            return ToolResult(f"Jira refused the request: {exc}", is_error=True)
        except (ValueError, httpx.HTTPError) as exc:
            return ToolResult(f"Jira error: {exc}", is_error=True)
        return ToolResult(f"Unknown operation {operation}.", is_error=True)

    def _line(self, ctx: ToolContext, credential: Credential, issue: dict[str, Any]) -> str:
        f = issue.get("fields") or {}
        key = str(issue.get("key"))
        project = key.split("-")[0]
        site = credential.details.get("site_url") or ""
        source = Source(
            ref=f"jira:{key}",
            channel_external_id="",
            channel_name=f"Jira {project}",
            visibility="restricted",
            ts="",
            label=f"{key}",
            permalink=f"{site}/browse/{key}" if site else None,
            kind="jira",
            audience=f"jira:{project}",
        )
        short = ctx.refs.add(source)
        assignee = (f.get("assignee") or {}).get("displayName") or "unassigned"
        meta = " · ".join(
            x
            for x in [
                (f.get("status") or {}).get("name"),
                (f.get("priority") or {}).get("name"),
                f"assignee {assignee}",
                f"updated {str(f.get('updated', ''))[:10]}",
            ]
            if x
        )
        return f"[{short}] ({meta}) {key}: {f.get('summary', '')}"

    async def _search(self, ctx: ToolContext, credential: Credential, args: dict[str, Any]) -> ToolResult:
        jql = str(args.get("jql") or "").strip()
        if not jql:
            clauses = []
            project = str(args.get("project") or "").strip().upper()
            if project:
                if not PROJECT_RE.match(project):
                    raise ValueError(f"{project} is not a project key")
                clauses.append(f"project = {project}")
            if str(args.get("text") or "").strip():
                clauses.append(f"text ~ {_quote(str(args['text']).strip())}")
            jql = " AND ".join(clauses)
        limit = max(1, min(int(args.get("limit") or 15), 25))
        body = {
            "jql": f"{jql} ORDER BY updated DESC" if "order by" not in jql.lower() else jql,
            "maxResults": limit,
            "fields": FIELDS,
        }
        found = (await self._call(ctx, credential, "POST", "search/jql", json=body) or {}).get("issues") or []
        allowed, note = await self._filter(ctx, credential, [str(i["key"]).split("-")[0] for i in found])
        visible = [i for i in found if str(i["key"]).split("-")[0] in allowed]
        hidden = len(found) - len(visible)
        lines = [f"Jira issues for {jql or 'everything'} ({len(visible)}):"]
        lines += [self._line(ctx, credential, i) for i in visible]
        if hidden:
            lines.append(f"({hidden} more issue(s) are in projects you can't browse.)")
        if note:
            lines.append(note)
        if not visible and not hidden and not note:
            lines.append("No issues match.")
        site = credential.details.get("site_url") or ""
        issues = [
            {
                "key": str(i["key"]),
                "summary": str((i.get("fields") or {}).get("summary") or ""),
                "status": str(((i.get("fields") or {}).get("status") or {}).get("name") or ""),
                "done": (((i.get("fields") or {}).get("status") or {}).get("statusCategory") or {}).get("key")
                == "done",
                "url": f"{site}/browse/{i['key']}" if site else None,
            }
            for i in visible
        ]
        return ToolResult(cap(redact("\n".join(lines))[0]), data={"issues": issues})

    async def _get(self, ctx: ToolContext, credential: Credential, key: str) -> ToolResult:
        if not KEY_RE.match(key):
            return ToolResult(f"{key or 'That'} is not an issue key like PAY-101.", is_error=True)
        allowed, note = await self._filter(ctx, credential, [key.split("-")[0]])
        if note:
            return ToolResult(note, is_error=True)
        if key.split("-")[0] not in allowed:
            return ToolResult(f"Access denied: you can't browse the project of {key}.", is_error=True)
        issue = await self._call(ctx, credential, "GET", f"issue/{key}", params={"fields": ",".join(FIELDS)})
        if issue is None:
            return ToolResult(f"{key} does not exist or is not visible.", is_error=True)
        description = adf_text((issue.get("fields") or {}).get("description")).strip()
        lines = [self._line(ctx, credential, issue)]
        if description:
            lines.append("  Description: " + description.replace("\n", " ⏎ ")[:3000])
        return ToolResult(cap(redact("\n".join(lines))[0]))

    async def _all(
        self, ctx: ToolContext, credential: Credential, jql: str
    ) -> tuple[list[dict[str, Any]], bool]:
        """Every issue a query matches, page by page, up to MAX_REPORT_ISSUES (then truncated)."""
        issues: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            body: dict[str, Any] = {"jql": jql, "maxResults": 100, "fields": REPORT_FIELDS}
            if token:
                body["nextPageToken"] = token
            page = await self._call(ctx, credential, "POST", "search/jql", json=body) or {}
            issues += page.get("issues") or []
            token = page.get("nextPageToken")
            if page.get("isLast", True) or not token:
                return issues, False
            if len(issues) >= MAX_REPORT_ISSUES:
                return issues[:MAX_REPORT_ISSUES], True

    async def _report_issues(
        self, ctx: ToolContext, credential: Credential, args: dict[str, Any]
    ) -> ToolResult:
        """Figures are computed by the caller from ``data``; ``text`` lists the issues with refs for the
        narrative. Created and resolved times are exact timestamps: the date bound in the JQL is a day
        early because Jira reads date literals in its account's time zone."""
        project = str(args.get("project") or "").strip().upper()
        if not PROJECT_RE.match(project):
            raise ValueError(f"{project or 'That'} is not a project key")
        since = datetime.fromisoformat(str(args.get("since")).replace("Z", "+00:00"))
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        allowed, note = await self._filter(ctx, credential, [project])
        if note or project not in allowed:
            message = note or f"Access denied: you can't browse the {project} project."
            return ToolResult(message, is_error=True, data={"denied": True})
        day = (since.astimezone(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
        open_now, cut_open = await self._all(
            ctx, credential, f"project = {project} AND statusCategory != Done"
        )
        recent, cut_recent = await self._all(ctx, credential, f'project = {project} AND updated >= "{day}"')
        found = {str(i["key"]): i for i in [*open_now, *recent]}
        issues = sorted(
            found.values(), key=lambda i: str((i.get("fields") or {}).get("updated", "")), reverse=True
        )
        data = {
            "project": project,
            "issues": [_report_fields(i) for i in issues],
            "truncated": cut_open or cut_recent,
        }
        lines = [f"Issues of {project} (open now, or updated since {day}): {len(issues)}"]
        lines += [self._line(ctx, credential, i) for i in issues]
        return ToolResult(cap(redact("\n".join(lines))[0]), data=data)

    async def _components(self, ctx: ToolContext, credential: Credential, project: str) -> ToolResult:
        if not PROJECT_RE.match(project):
            raise ValueError(f"{project or 'That'} is not a project key")
        allowed, note = await self._filter(ctx, credential, [project])
        if note or project not in allowed:
            return ToolResult(
                note or f"Access denied: you can't browse the {project} project.", is_error=True
            )
        found = await self._call(ctx, credential, "GET", f"project/{project}/components") or []
        lines = [f"Components of {project}:"]
        for c in found:
            lead = (c.get("lead") or {}).get("displayName") or "no lead"
            lines.append(f"{c.get('name')} (lead {lead}): {c.get('description') or ''}".rstrip(": "))
        data = {
            "project": project,
            "components": [
                {
                    "name": c.get("name"),
                    "lead": (c.get("lead") or {}).get("accountId"),
                    "description": c.get("description"),
                }
                for c in found
            ],
        }
        return ToolResult(cap(redact("\n".join(lines if found else [*lines, "none"]))[0]), data=data)

    async def _assignee(self, ctx: ToolContext, credential: Credential, email: str) -> str | None:
        if not email:
            return None
        account = await self._account_for_email(ctx, credential, email)
        if account is None:
            raise JiraError(f"no Jira user has the email {email}")
        return account

    def _issue_link(self, credential: Credential, key: str) -> str:
        site = credential.details.get("site_url") or ""
        return f"{site}/browse/{key}" if site else key

    async def _create(self, ctx: ToolContext, credential: Credential, args: dict[str, Any]) -> ToolResult:
        project = str(args.get("project") or "").strip().upper()
        summary = " ".join(str(args.get("summary") or "").split())
        if not PROJECT_RE.match(project):
            raise ValueError(f"{project or 'That'} is not a project key")
        if not 1 <= len(summary) <= 255:
            raise ValueError("a summary of 1 to 255 characters is required")
        label = marker(str(args.get("_idempotency_key") or f"{project}:{summary}"))
        earlier = await self._call(
            ctx, credential, "POST", "search/jql", json={"jql": f'labels = "{label}"', "fields": ["summary"]}
        )
        if earlier and earlier.get("issues"):  # created by an earlier attempt of this action
            key = str(earlier["issues"][0]["key"])
            url = self._issue_link(credential, key)
            return ToolResult(
                f"{key} exists already: {url}", data={"target_ref": f"jira:{key}", "key": key, "url": url}
            )
        assignee = await self._assignee(ctx, credential, str(args.get("assignee_email") or "").strip())
        labels = [label, *(LABEL_RE.sub("-", str(x))[:50] for x in (args.get("labels") or [])[:5])]
        fields: dict[str, Any] = {
            "project": {"key": project},
            "issuetype": {"name": str(args.get("issue_type") or "Bug")},
            "summary": summary,
            "description": adf_doc(str(args.get("description") or "")),
            "labels": labels,
        }
        if assignee:
            fields["assignee"] = {"accountId": assignee}
        if str(args.get("component") or "").strip():
            fields["components"] = [{"name": str(args["component"]).strip()}]
        created = await self._call(ctx, credential, "POST", "issue", json={"fields": fields})
        if not created or not created.get("key"):
            raise JiraError("Jira did not create the issue")
        key = str(created["key"])
        url = self._issue_link(credential, key)
        return ToolResult(
            f"Created {key}: {summary} ({url})", data={"target_ref": f"jira:{key}", "key": key, "url": url}
        )

    async def _assign(self, ctx: ToolContext, credential: Credential, args: dict[str, Any]) -> ToolResult:
        key = str(args.get("key") or "").strip().upper()
        if not KEY_RE.match(key):
            return ToolResult(f"{key or 'That'} is not an issue key like PAY-101.", is_error=True)
        email = str(args.get("assignee_email") or "").strip()
        assignee = await self._assignee(ctx, credential, email)
        await self._call(ctx, credential, "PUT", f"issue/{key}/assignee", json={"accountId": assignee})
        url = self._issue_link(credential, key)
        return ToolResult(
            f"Assigned {key} to {email}", data={"target_ref": f"jira:{key}", "key": key, "url": url}
        )

    async def _comment(self, ctx: ToolContext, credential: Credential, args: dict[str, Any]) -> ToolResult:
        key = str(args.get("key") or "").strip().upper()
        text = str(args.get("text") or "").strip()
        if not KEY_RE.match(key):
            return ToolResult(f"{key or 'That'} is not an issue key like PAY-101.", is_error=True)
        if not text:
            raise ValueError("a comment needs text")
        ref = marker(str(args.get("_idempotency_key") or f"{key}:{text}"))
        existing = await self._call(ctx, credential, "GET", f"issue/{key}/comment")
        if existing is None:
            return ToolResult(f"{key} does not exist or is not visible.", is_error=True)
        url = self._issue_link(credential, key)
        if any(ref in adf_text(c.get("body")) for c in existing.get("comments") or []):
            return ToolResult(
                f"Commented on {key} already", data={"target_ref": f"jira:{key}", "key": key, "url": url}
            )
        body = adf_doc(f"{text}\n\nvia the agent · {ref}")
        await self._call(ctx, credential, "POST", f"issue/{key}/comment", json={"body": body})
        return ToolResult(f"Commented on {key}", data={"target_ref": f"jira:{key}", "key": key, "url": url})

    async def _projects(self, ctx: ToolContext, credential: Credential) -> ToolResult:
        values = (await self._call(ctx, credential, "GET", "project/search") or {}).get("values") or []
        allowed, note = await self._filter(ctx, credential, [str(p["key"]) for p in values])
        visible = [p for p in values if p["key"] in allowed]
        lines = [f"{p['key']}: {p.get('name', '')}" for p in visible]
        if note:
            lines.append(note)
        data = {"projects": [{"key": p["key"], "name": p.get("name", "")} for p in visible]}
        return ToolResult("Jira projects you can browse:\n" + ("\n".join(lines) or "none"), data=data)


def marker(idempotency_key: str) -> str:
    """The label (or comment reference) a write carries, so a retry can find what it did."""
    return "agent-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:12]


URL_RE = re.compile(r"https?://[^\s<>|]+[^\s<>|.,;:!?)\]]")


def adf_line(line: str) -> list[dict[str, Any]]:
    """One line as ADF text nodes, URLs as links."""
    nodes: list[dict[str, Any]] = []
    at = 0
    for m in URL_RE.finditer(line):
        if m.start() > at:
            nodes.append({"type": "text", "text": line[at : m.start()]})
        nodes.append(
            {"type": "text", "text": m.group(0), "marks": [{"type": "link", "attrs": {"href": m.group(0)}}]}
        )
        at = m.end()
    if at < len(line):
        nodes.append({"type": "text", "text": line[at:]})
    return nodes


def adf_doc(text: str) -> dict[str, Any]:
    """Plain text as Atlassian Document Format: blank lines separate paragraphs, line breaks are kept
    and URLs become links."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()] or [""]
    content = []
    for paragraph in paragraphs:
        nodes: list[dict[str, Any]] = []
        for i, line in enumerate(paragraph.splitlines()):
            if i:
                nodes.append({"type": "hardBreak"})
            nodes += adf_line(line)
        content.append({"type": "paragraph", "content": nodes})
    return {"type": "doc", "version": 1, "content": content}


def _report_fields(issue: dict[str, Any]) -> dict[str, Any]:
    f = issue.get("fields") or {}
    status = f.get("status") or {}
    return {
        "key": str(issue.get("key")),
        "status": status.get("name"),
        "category": (status.get("statusCategory") or {}).get("key"),
        "priority": (f.get("priority") or {}).get("name"),
        "assignee": (f.get("assignee") or {}).get("displayName"),
        "created": f.get("created"),
        "resolved": f.get("resolutiondate"),
        "updated": f.get("updated"),
    }


class JiraError(Exception):
    pass

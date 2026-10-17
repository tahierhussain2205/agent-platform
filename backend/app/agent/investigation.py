"""Investigation and planning (Example 2, part 1; spec 7.2 to 7.4 and 7.7).

1. **Orient**, in code: the Jira issue the request names, the catalog service it concerns (with its
   repository and log source), the incident's window, and the session's current plan. Every later
   query is driven by this case file, not by the model's guesses.
2. **Investigate**, in parallel: one isolated subagent per kind (logs, code, changes), each with its
   own context, only its own tools and the fixed findings schema. Code collects facts from the tool
   results themselves (error signatures, files read, commits seen) to check the plan and to measure
   retrieval.
3. **Plan**, from the findings, on the template of spec 7.3, then checked in code before anyone sees
   it (``check_plan``): only cited evidence counts, a hypothesis without evidence is investigated
   first, bug work reproduces first, and confidence never exceeds the evidence.

The plan is stored as the session's current plan; a follow-up in the thread revises it.
"""

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.agent import prompts
from app.agent.tickets import without_refs
from app.agent.types import CaseFile, Composed, Findings, TaskRef, Understanding
from app.db.tenant import tenant_tx
from app.llm.gateway import BudgetExceededError
from app.policy.redaction import redact
from app.slack.adapter import escape
from app.tools.base import RefBook, tool_name
from app.tools.executor import ExecResult
from app.tools.registry import Registry

if TYPE_CHECKING:
    from app.agent.runner import AgentRunner

log = logging.getLogger(__name__)
KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d+$")
MAX_ISSUES = 3  # issues one plan covers
LEAD = timedelta(hours=2)  # a window starts this long before the issue was created
LOOKBACK = timedelta(hours=24)
MIN_BRIEF_WINDOW = timedelta(hours=1)  # shorter is "now", not a window (a request in the present tense)
MAX_WINDOW = timedelta(days=7)
LEVELS = ("low", "medium", "high")
STEP_KINDS = ("investigate", "reproduce", "implement", "verify", "other")
TOOLS = {
    "logs": ("catalog.find_owners", "observability.*"),
    "code": (
        "github.search_code",
        "github.read_code",
        "github.list_files",
        "github.read_file",
        "github.get_codeowners",
    ),
    "changes": ("github.list_commits", "github.get_commit", "github.list_deployments"),
}
PREFIX = {"logs": "L", "code": "K", "changes": "G"}


def _when(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def window_of(time_window: dict[str, Any] | None, issue: dict[str, Any] | None, now: datetime) -> list[str]:
    """The brief's window when it has one; otherwise from the issue (two hours before it was
    created, to its resolution or now); otherwise the last day. At most seven days, never ahead."""
    tw = time_window or {}
    start, end = _when(tw.get("start")), _when(tw.get("end"))
    if start is not None and (end or now) - start < MIN_BRIEF_WINDOW:
        start, end = None, None
    if issue and start is None and _when(issue.get("created")):
        start = _when(issue.get("created"))
        start = start - LEAD if start else None
    if issue and end is None and str(issue.get("status")).lower() in ("done", "closed", "resolved"):
        updated = _when(issue.get("updated"))
        end = updated + timedelta(hours=1) if updated else None
    end = min(end or now, now)
    start = max(start or end - LOOKBACK, end - MAX_WINDOW)
    if start >= end:
        start = end - LOOKBACK
    return [start.isoformat(), end.isoformat()]


def allowed_for(kind: str, name: str, registry: Registry) -> bool:
    found = registry.lookup(name)
    if found is None:
        return False
    adapter, op = found
    for entry in TOOLS[kind]:
        owner, _, operation = entry.partition(".")
        if owner in (adapter.key, getattr(adapter, "family", None)) and operation in ("*", op.name):
            return True
    return False


def facts_of(kind: str, seen: list[tuple[str, ExecResult]]) -> dict[str, Any]:
    """What the investigator's tools returned, as data (never the model's account of it)."""
    signatures: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    commits: list[dict[str, Any]] = []
    for name, result in seen:
        if result.is_error or result.denied:
            continue
        data = result.data
        for g in data.get("groups") or []:
            if g.get("signature") and not any(s["signature"] == g["signature"] for s in signatures):
                signatures.append(
                    {
                        k: g.get(k)
                        for k in (
                            "signature",
                            "count",
                            "first_seen",
                            "last_seen",
                            "top_frame",
                            "exception_type",
                        )
                    }
                )
        repo = data.get("repo")
        if name.endswith("read_code") and data.get("path"):
            files.append({"repo": repo, "path": data["path"], "lines": [data.get("start"), data.get("end")]})
        elif name.endswith("search_code"):
            files += [{"repo": repo, "path": p, "matched": True} for p in data.get("files") or []]
        elif name.endswith("read_file") and data.get("path"):
            files.append({"repo": repo, "path": data["path"]})
        for c in data.get("commits") or []:
            commits.append(
                {"repo": repo, "sha": c.get("sha"), "message": c.get("message"), "files": c.get("files")}
            )
        if name.endswith("get_commit") and data.get("sha"):
            commits.append({"repo": repo, "sha": data["sha"], "files": data.get("files")})
    out: dict[str, Any] = {}
    if signatures:
        out["signatures"] = signatures[:20]
    if files:
        out["files"] = files[:60]
    if commits:
        out["commits"] = commits[:40]
    return out


def plan_issue_keys(plan: dict[str, Any]) -> list[str]:
    """The issues a stored plan is about (plans before several issues kept one, as ``issue``)."""
    case = plan.get("case") or {}
    found = [str(i.get("key")) for i in case.get("issues") or [] if isinstance(i, dict) and i.get("key")]
    single = (case.get("issue") or {}).get("key")
    return found or ([str(single)] if single else [])


def case_for_model(case: CaseFile) -> dict[str, Any]:
    out: dict[str, Any] = {"goal": case.goal, "window": {"start": case.window[0], "end": case.window[1]}}
    issues = case.issues or ([case.issue] if case.issue else [])
    if issues:
        out["issues"] = [
            {
                **{k: i.get(k) for k in ("key", "summary", "status", "created", "components", "url")},
                "description": str(i.get("description") or "")[: 1500 // len(issues)],
            }
            for i in issues
        ]
    if case.service:
        out["service"] = case.service
    if case.prior_plan:
        out["current_plan"] = {
            "goal": case.prior_plan.get("goal"),
            "hypotheses": [h.get("text") for h in case.prior_plan.get("hypotheses") or []][:5],
        }
    if case.notes:
        out["not_found"] = case.notes
    if case.code_at:
        out["code_as_of"] = case.code_at  # the code is read as it was when the incident started
    return out


# ------------------------------------------------------------------ the plan, checked in code


def _numbers(values: Any, total: int) -> list[int]:
    out: list[int] = []
    for v in values or []:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= total and n not in out:
            out.append(n)
    return out


def _text(value: Any, limit: int = 500) -> str:
    return redact(" ".join(str(value or "").split()))[0][:limit]


def check_plan(
    raw: dict[str, Any], total: int, kinds: dict[int, str], seen_files: set[str]
) -> tuple[dict[str, Any], list[str]]:
    """The plan as it may be shown, and what could not be verified. ``total`` is the number of
    citable findings sources, ``kinds`` their kind (logs, code, changes, jira, ...) by number,
    ``seen_files`` the ``repo:path`` the investigators actually read."""
    not_verified: list[str] = []
    evidence = []
    for e in (raw.get("evidence") or [])[:12]:
        text, cited = (
            _text(e.get("text") if isinstance(e, dict) else e),
            _numbers((e or {}).get("sources") if isinstance(e, dict) else [], total),
        )
        if not text:
            continue
        if cited:
            evidence.append({"text": text, "sources": cited})
        else:
            not_verified.append(f"Evidence without a source: {text[:120]}")
    hypotheses = []
    for h in (raw.get("hypotheses") or [])[:5]:
        if not isinstance(h, dict) or not _text(h.get("text")):
            continue
        cited = _numbers(h.get("evidence"), total)
        level = str(h.get("confidence") or "low")
        level = level if level in LEVELS else "low"
        if not cited:
            level = "low"
        elif level == "high" and len({kinds.get(n, "?") for n in cited}) < 2:
            level = "medium"  # high needs independent sources (logs and code, code and changes, ...)
        hypotheses.append(
            {
                "text": _text(h.get("text")),
                "confidence": level,
                "evidence": cited,
                "confirm": _text(h.get("confirm"), 300),
            }
        )
    hypotheses.sort(key=lambda h: (LEVELS.index(h["confidence"]), len(h["evidence"])), reverse=True)
    steps = []
    for s in (raw.get("steps") or [])[:12]:
        text = _text(s.get("text") if isinstance(s, dict) else s, 400)
        kind = str((s or {}).get("kind") if isinstance(s, dict) else "other")
        if text:
            steps.append({"text": text, "kind": kind if kind in STEP_KINDS else "other"})
    unproven = bool(hypotheses) and not hypotheses[0]["evidence"]
    if unproven and (not steps or steps[0]["kind"] != "investigate"):
        # The core assumption has no evidence: the next step is investigation, not implementation.
        steps.insert(
            0,
            {"text": f"Confirm or rule out first: {hypotheses[0]['text'][:200]}", "kind": "investigate"},
        )
    first_change = next((i for i, s in enumerate(steps) if s["kind"] == "implement"), None)
    if first_change is not None and not any(s["kind"] == "reproduce" for s in steps[:first_change]):
        steps.insert(
            first_change,
            {
                "text": "Write a failing test that reproduces the problem before changing code.",
                "kind": "reproduce",
            },
        )
    files = []
    for f in (raw.get("files") or [])[:15]:
        if not isinstance(f, dict) or not f.get("path"):
            continue
        repo, path = _text(f.get("repo"), 200), _text(f.get("path"), 300).lstrip("/")
        files.append(
            {
                "repo": repo,
                "path": path,
                "why": _text(f.get("why"), 200),
                "read": f"{repo}:{path}" in seen_files,
            }
        )
    for f in files:
        if not f["read"]:
            not_verified.append(f"{f['repo']} {f['path']} was not read in this investigation")
    level = str(raw.get("confidence") or "low")
    level = level if level in LEVELS else "low"
    top = hypotheses[0]["confidence"] if hypotheses else "low"
    level = LEVELS[min(LEVELS.index(level), LEVELS.index(top))]
    revision: dict[str, Any] = raw["revision"] if isinstance(raw.get("revision"), dict) else {}
    plan = {
        "summary": _text(raw.get("summary"), 800),
        "goal": _text(raw.get("goal"), 300),
        "evidence": evidence,
        "hypotheses": hypotheses,
        "steps": steps,
        "files": files,
        "risks": [_text(r, 300) for r in (raw.get("risks") or [])[:6] if _text(r)],
        "verification": _text(raw.get("verification"), 500),
        "rollback": _text(raw.get("rollback"), 500),
        "open_questions": [_text(q, 300) for q in (raw.get("open_questions") or [])[:6] if _text(q)],
        "confidence": level,
        "revision": {
            "changed": bool(revision.get("changed")),
            "what_changed": _text(revision.get("what_changed"), 400),
        },
    }
    return plan, not_verified


def render(plan: dict[str, Any]) -> str:
    """The plan for Slack (model text escaped; citations as [n])."""

    def cites(numbers: list[int]) -> str:
        return "".join(f" [{n}]" for n in numbers)

    keys = [
        f"<{i['url']}|{i['key']}>" if i.get("url") else str(i["key"])  # our links: kept as they are
        for i in plan.get("issues") or []
    ]
    subject = f"Plan for {', '.join(keys)}" if keys else "Plan"
    lines = [f"*{subject}: {escape(plan['goal'])}* · confidence {plan['confidence']}"]
    if plan["summary"]:
        lines.append(escape(plan["summary"]))
    if plan["revision"]["changed"] and plan["revision"]["what_changed"]:
        lines.append(f"_Revised: {escape(plan['revision']['what_changed'])}_")
    if plan["evidence"]:
        lines += ["*Evidence*", *(f"• {escape(e['text'])}{cites(e['sources'])}" for e in plan["evidence"])]
    if plan["hypotheses"]:
        lines.append("*Hypotheses*")
        for n, h in enumerate(plan["hypotheses"], 1):
            confirm = f" Confirm: {escape(h['confirm'])}" if h["confirm"] else ""
            lines.append(f"{n}. {escape(h['text'])} ({h['confidence']}){cites(h['evidence'])}.{confirm}")
    if plan["steps"]:
        lines += [
            "*Steps*",
            *(f"{n}. [{s['kind']}] {escape(s['text'])}" for n, s in enumerate(plan["steps"], 1)),
        ]
    if plan["files"]:
        lines.append("*Files*")
        lines += [
            f"• {escape(f['repo'])} `{escape(f['path'])}`"
            + (f": {escape(f['why'])}" if f["why"] else "")
            + ("" if f["read"] else " _(not read yet)_")
            for f in plan["files"]
        ]
    if plan["risks"]:
        lines += ["*Risks*", *(f"• {escape(r)}" for r in plan["risks"])]
    if plan["verification"]:
        lines += ["*Verification*", escape(plan["verification"])]
    if plan["rollback"]:
        lines += ["*Rollback*", escape(plan["rollback"])]
    if plan["open_questions"]:
        lines += ["*Open questions*", *(f"• {escape(q)}" for q in plan["open_questions"])]
    return "\n".join(lines)


def renumber(text: str, total: int) -> tuple[str, list[int]]:
    """Citations renumbered in order of first use: (text, old numbers in their new order)."""
    order: list[int] = []
    for m in re.finditer(r"\[(\d{1,3})\]", text):
        n = int(m.group(1))
        if 1 <= n <= total and n not in order:
            order.append(n)
    remap = {old: new for new, old in enumerate(order, 1)}
    text = re.sub(
        r" ?\[(\d{1,3})\]", lambda m: f" [{remap[int(m.group(1))]}]" if int(m.group(1)) in remap else "", text
    )
    return text, order


# ------------------------------------------------------------------ the steps


class Investigation:
    def __init__(self, runner: "AgentRunner") -> None:
        self.r = runner

    async def orient(self, ref: TaskRef, u: Understanding) -> CaseFile:
        from app.agent.runner import playbook_of

        _, brief = playbook_of(u)
        ctx = await self.r._context(ref)
        refs = RefBook("C")
        _, registry, _, executor = await self.r._tools(ref, ctx, u.mode, refs)
        await self.r.step(ref, "gather", "running", "Finding the issue, the service and the window")
        entities = brief.get("entities") or {}
        notes: list[str] = []
        points: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        prior = await self.r._prior_plan(ref)
        keys = [str(k).upper() for k in entities.get("ids") or [] if KEY_RE.match(str(k))]
        if not keys and prior:  # a follow-up in the thread: the issues the current plan is about
            keys = plan_issue_keys(prior)
        if not keys:  # "these tickets": the ones the agent created earlier in this thread
            keys = await self.r._session_tickets(ref)
        for key in list(dict.fromkeys(keys))[:MAX_ISSUES]:
            if "jira" not in registry.adapters:
                notes.append(f"{key}: Jira is not connected")
                break
            before = set(refs.by_ref)
            found = await executor.run(tool_name("jira", "get_issue"), {"key": key})
            if not found.is_error and found.data.get("key"):
                issues.append(found.data)
                points += self._points(
                    refs, before, f"{key} ({found.data.get('status')}): {found.data.get('summary')}"
                )
                continue
            notes.append(f"{key}: {found.text[:200]}")
        issue = issues[0] if issues else None
        text = " ".join(
            str(x)
            for x in (
                brief.get("goal"),
                *(
                    f"{i.get('summary')} {str(i.get('description') or '')[: 600 // len(issues)]}"
                    for i in issues
                ),
            )
            if x
        )
        args: dict[str, Any] = {"text": text[:2000]}
        if issue:
            args["project"] = str(issue["key"]).split("-")[0]
            if issue.get("components"):
                args["component"] = issue["components"][0]
        for repo in entities.get("repositories") or []:
            args["repository"] = repo
        before = set(refs.by_ref)
        owners = await executor.run(tool_name("catalog", "find_owners"), args)
        service = await self._service(ref, (owners.data.get("services") or [{}])[0].get("name"))
        if service is None:
            notes.append(
                "No confirmed service in the catalog matches, so logs and the repository are unknown"
            )
        else:
            if not service.get("repository") and entities.get("repositories"):
                service["repository"] = entities["repositories"][0]
            candidates = owners.data.get("candidates") or []
            service["owners"] = [f"{c['name']} ({c['email']})" for c in candidates[:3]]
            points += self._points(
                refs,
                before,
                f"Service {service['name']}: repository {service.get('repository') or 'unknown'}, "
                f"logs {'connected' if service.get('logs') else 'not set'}",
            )
            if not service.get("logs"):
                notes.append(f"{service['name']} has no log source in the catalog")
            if not service.get("repository"):
                notes.append(f"{service['name']} has no repository in the catalog")
        window = window_of(entities.get("time_window"), issue, self.r.d.now())
        # An incident that ended a while ago happened in the code as it was then, not as it is now.
        past = self.r.d.now() - datetime.fromisoformat(window[1]) > timedelta(hours=1)
        await self.r.step(
            ref,
            "gather",
            "completed",
            f"{', '.join(str(i['key']) for i in issues) or 'No issue'}, "
            f"{(service or {}).get('name') or 'no service'}",
        )
        return CaseFile(
            str(brief.get("goal") or ""),
            window,
            issue,
            service,
            prior,
            points,
            notes,
            window[0] if past else None,
            issues,
        )

    @staticmethod
    def _points(refs: RefBook, before: set[str], text: str) -> list[dict[str, Any]]:
        """A case finding citing the sources a call added to the ref book."""
        sources = [s.to_dict() for short, s in refs.by_ref.items() if short not in before]
        return [{"text": redact(text)[0][:600], "sources": sources}] if sources else []

    async def _service(self, ref: TaskRef, name: Any) -> dict[str, Any] | None:
        if not name:
            return None
        async with tenant_tx(self.r.d.pool, ref.org_id) as conn:
            row = await (
                await conn.execute(
                    "select s.name, r.full_name, s.jira_project, s.jira_component, s.log_source, s.paths "
                    "from app.services s left join app.repositories r on r.id = s.repository_id "
                    "where s.status = 'active' and s.name = %s",
                    (name,),
                )
            ).fetchone()
        if row is None:
            return None
        return {
            "name": row[0],
            "repository": row[1],
            "jira_project": row[2],
            "jira_component": row[3],
            "logs": bool((row[4] or {}).get("provider")),
            "paths": list(row[5] or []),
        }

    async def investigate(
        self, ref: TaskRef, u: Understanding, case: CaseFile, kind: str, tier: str
    ) -> Findings:
        from app.agent.runner import brief_for_model, playbook_of

        spec, brief = playbook_of(u)
        ctx = await self.r._context(ref)
        refs = RefBook(PREFIX[kind])
        _, registry, _, executor = await self.r._tools(ref, ctx, u.mode, refs)
        tools = [
            d for d in self.r._definitions(registry, spec, "read") if allowed_for(kind, d["name"], registry)
        ]
        if not tools:
            missing = {
                "logs": "No log backend is connected",
                "code": "GitHub is not connected",
                "changes": "GitHub is not connected",
            }[kind]
            return Findings([], [f"{kind}: {missing}"], kind=kind)
        await self.r.step(ref, "gather", "running", f"Investigating {kind}")
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": f"<brief>{json.dumps(brief_for_model(brief))}</brief>\n"
                f"<case>{json.dumps(case_for_model(case))}</case>\n<now>{self.r.d.now().isoformat()}</now>",
            }
        ]
        seen: list[tuple[str, ExecResult]] = []
        if kind == "code" and case.code_at and (case.service or {}).get("repository"):
            await self._pin(executor, str(case.service["repository"]), case.code_at)  # type: ignore[index]
        system = prompts.system(ctx.org_name, spec, ctx.roles, prompts.INVESTIGATE[kind])
        tools.append(prompts.submit_tool("submit_findings", "Submit findings."))
        found = await self.r._subagent(
            ref, tier, executor, tools, system, messages, refs, f"investigate_{kind}", seen
        )
        found.kind, found.facts = kind, facts_of(kind, seen)
        summary = f"{kind}: {len(found.points)} findings" + (
            f", {len(found.gaps)} gaps" if found.gaps else ""
        )
        await self.r.step(ref, "gather", "failed" if found.stopped else "completed", summary)
        return found

    @staticmethod
    async def _pin(executor: Any, repo: str, when: str) -> None:
        """Read a past incident's code at the last commit before it (through the policy, like any
        read); the code tools then build the sandbox at that commit."""
        listed = await executor.run(tool_name("github", "list_commits"), {"repo": repo, "until": when})
        commits = listed.data.get("commits") or []
        if commits:
            executor.ctx.cache[f"github:base:{repo}"] = str(commits[0]["sha"])

    async def plan(
        self, ref: TaskRef, u: Understanding, case: CaseFile, findings: list[Findings], tier: str
    ) -> Composed:
        from app.agent.runner import as_tier, brief_for_model, playbook_of

        spec, brief = playbook_of(u)
        ctx = await self.r._context(ref)
        numbered: list[dict[str, Any]] = []
        index: dict[str, int] = {}
        kinds: dict[int, str] = {}
        model_points = []
        sets = [("case", case.points), *((f.kind or "gather", f.points) for f in findings)]
        for kind, points in sets:
            for p in points:
                numbers = []
                for source in p["sources"]:
                    if source["ref"] not in index:
                        numbered.append(source)
                        index[source["ref"]] = len(numbered)
                        kinds[len(numbered)] = kind if kind != "case" else str(source.get("kind") or "case")
                    numbers.append(index[source["ref"]])
                model_points.append({"text": p["text"], "sources": numbers, "from": kind})
        # Said to people: without our refs (demo run 4: "… [L1]" in a "Not covered" line).
        gaps = [without_refs(g) for g in (*case.notes, *(g for f in findings for g in f.gaps))]
        facts: dict[str, Any] = {}
        for f in findings:
            for key, values in f.facts.items():
                facts.setdefault(key, []).extend(values)
        seen_files = {f"{x.get('repo')}:{x.get('path')}" for x in facts.get("files") or []}
        seen_files |= {
            f"{c.get('repo')}:{p}" for c in facts.get("commits") or [] for p in c.get("files") or []
        }
        await self.r.step(ref, "plan", "running", "Writing the plan")
        content = (
            f"<brief>{json.dumps(brief_for_model(brief))}</brief>\n<case>{json.dumps(case_for_model(case))}</case>\n"
            f"<findings>{json.dumps({'points': model_points, 'gaps': gaps})}</findings>"
        )
        if case.prior_plan:
            prior = {k: case.prior_plan.get(k) for k in ("goal", "summary", "hypotheses", "steps", "files")}
            content += f"\n<prior_plan>{json.dumps(prior)}</prior_plan>"
        try:
            for attempt in (1, 2):  # a plan with nothing in it is asked for once more (demo run 4)
                result = await self.r.d.gateway.complete(
                    self.r._call(ref, "plan"),
                    as_tier(tier),
                    system=prompts.system(ctx.org_name, spec, ctx.roles, prompts.INVESTIGATION_PLAN),
                    messages=[{"role": "user", "content": content}],
                    tools=[prompts.submit_tool("submit_investigation_plan", "Submit the plan.")],
                    tool_choice={"type": "tool", "name": "submit_investigation_plan"},
                    max_tokens=16000,  # a whole plan, and the model's thinking before it
                )
                raw = result.tool_input("submit_investigation_plan")
                if any(raw.get(k) for k in ("evidence", "hypotheses", "steps")):
                    break
                shape = {k: type(v).__name__ for k, v in raw.items()}  # names and types, never content
                log.warning("the plan came back empty", extra={"attempt": attempt, "shape": shape})
        except BudgetExceededError as exc:
            listed = "\n".join(
                f"• {escape(p['text'])}" + "".join(f" [{n}]" for n in p["sources"]) for p in model_points
            )
            text, order = renumber(listed or "I had not found anything yet.", len(numbered))
            sources = await self.r._permalinks(ref, [numbered[n - 1] for n in order])
            return Composed(text, sources, "low", 0, len(model_points), [], stopped=str(exc))
        plan, not_verified = check_plan(raw, len(numbered), kinds, seen_files)
        if not (plan["evidence"] or plan["hypotheses"] or plan["steps"]):
            return Composed(
                "I couldn't write a plan this time: the answer I got had no plan in it. Ask me again.",
                [],
                "low",
                0,
                len(model_points),
                [f"Not covered: {g}" for g in gaps[:5]],
            )
        plan["issues"] = [
            {"key": str(i["key"]), "url": i.get("url")}
            for i in case.issues or ([case.issue] if case.issue else [])
        ]
        not_verified = [*(f"Not covered: {g}" for g in gaps[:5]), *not_verified]
        text, order = renumber(render(plan), len(numbered))
        sources = await self.r._permalinks(ref, [numbered[n - 1] for n in order])
        stored = {
            **plan,
            "playbook": spec.key,
            "version": spec.version,
            "case": case_for_model(case),
            "facts": facts,
            "sources": [numbered[n - 1]["ref"] for n in order],
        }
        await self.r._artifact(ref, "plan", stored, [s.get("permalink") or s["ref"] for s in sources])
        verified = len(plan["evidence"]) + sum(1 for h in plan["hypotheses"] if h["evidence"])
        total = verified + sum(1 for x in not_verified if x.startswith("Evidence without"))
        await self.r.step(
            ref,
            "verify",
            "completed",
            f"{len(plan['evidence'])} evidence items and {len(plan['hypotheses'])} hypotheses checked",
        )
        return Composed(text, sources, plan["confidence"], verified, total, not_verified[:8])

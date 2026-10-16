"""Implementation and pull requests (Example 2, part 2; spec 3.3 steps 4 and 5, section 8).

1. **Prepare**, in code: the thread's current plan, its repository, the base commit (the default
   branch's head for a new change, the pull request's head for a revision), the branch under
   ``agent/``, the pull request's feedback for a revision, and the task's working copy (a sandbox
   with a baseline commit). The requester must be able to push to the repository.
2. **Implement**: an isolated subagent with the read tools and the sandbox tools, told to
   reproduce first. It never sees a credential; its commands run with no network.
3. **Verify**, in code: the repository's tests in the sandbox, and whether the new test fails
   without the fix (the fix is stashed, the tests run, the fix comes back).
4. **Review**: a reviewer subagent that sees only the plan and the diff (never the implementer's
   reasoning, spec 7.4).
5. **Export**: the diff leaves the sandbox, is checked in code (``app.agent.changes``), stored as the
   task's ``diff`` artifact, and the sandbox is destroyed. The push is planned as an action.
6. **Report**: the pull request, the files, the checks, and what was and was not verified.

The push itself is the runner's ``execute`` of ``github.push_changes`` after the policy (and a
person, in Ask or when the checks ask) allowed it.
"""

import base64
import json
import logging
import re
import shlex
from dataclasses import asdict
from typing import TYPE_CHECKING, Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.agent import prompts, recipes
from app.agent.changes import TEST_FILE, check_change
from app.agent.types import (
    ActionOutcome,
    ChangeCase,
    Composed,
    Exported,
    Implemented,
    PlannedAction,
    ReviewResult,
    TaskRef,
    Understanding,
    Verification,
)
from app.db.tenant import tenant_tx
from app.llm.gateway import BudgetExceededError
from app.policy.redaction import redact
from app.sandbox import SandboxError, SandboxProvider, provider_from_settings
from app.sandbox.workspaces import Workspace, Workspaces, root_of
from app.slack.adapter import escape
from app.slack.client import SlackError
from app.tools.base import RefBook, Source, tool_name

if TYPE_CHECKING:
    from app.agent.runner import AgentRunner

log = logging.getLogger(__name__)
IMPLEMENTER_READS = {"github_read_code", "github_search_code", "github_list_files"}
IMPLEMENT_ROUNDS = 14
IMPLEMENT_TOKENS = 400_000
TEST_SECONDS = 600
INLINE_LIMIT = 200_000  # a diff artifact larger than this goes to the object store
MAX_STORED = 5_000_000
FAILED_STATES = ("failure", "timed_out", "cancelled", "action_required", "startup_failure")


def branch_name(issue_key: str | None, goal: str, task_id: str) -> str:
    """``agent/<issue>-<words of the goal>-<task>``: always under agent/, one branch per task."""
    slug = ""
    for word in re.findall(r"[a-z0-9]+", goal.lower()):
        if len(slug) + len(word) + 1 > 40:
            break
        slug = f"{slug}-{word}" if slug else word
    slug = slug or "change"
    task = task_id.replace("-", "")[-6:]
    return f"agent/{issue_key.lower()}-{slug}-{task}" if issue_key else f"agent/{slug}-{task}"


def plan_for_model(plan: dict[str, Any]) -> dict[str, Any]:
    keep = ("goal", "summary", "steps", "files", "verification", "risks")
    out = {k: plan.get(k) for k in keep if plan.get(k)}
    out["hypotheses"] = [
        {"text": h.get("text"), "confidence": h.get("confidence")} for h in (plan.get("hypotheses") or [])[:3]
    ]
    return out


def repository_of(plan: dict[str, Any]) -> str | None:
    for f in plan.get("files") or []:
        if f.get("repo"):
            return str(f["repo"])
    service = (plan.get("case") or {}).get("service") or {}
    return str(service["repository"]) if service.get("repository") else None


def tail(text: str, limit: int = 3000) -> str:
    return redact(text.strip()[-limit:])[0]


class Implementation:
    def __init__(self, runner: "AgentRunner") -> None:
        self.r = runner

    def _provider(self) -> SandboxProvider:
        provider = provider_from_settings(self.r.d.settings, self.r.d.http) if self.r.d.http else None
        if provider is None:
            raise SandboxError("no sandbox provider is configured")
        return provider

    async def _workspace(self, ref: TaskRef, case: ChangeCase) -> Workspace:
        found = await Workspaces(self.r.d.pool, self._provider()).find(
            UUID(ref.org_id), UUID(ref.task_id), case.repo
        )
        if found is None:
            raise SandboxError(f"this task has no working copy of {case.repo}")
        return found

    # ------------------------------------------------------------------ prepare

    async def prepare(self, ref: TaskRef, u: Understanding) -> ChangeCase:
        from app.agent.runner import playbook_of

        _, brief = playbook_of(u)
        ctx = await self.r._context(ref)
        _, registry, _, executor = await self.r._tools(ref, ctx, u.mode, RefBook("C"))
        await self.r.step(ref, "gather", "running", "Preparing the change")

        def stop(text: str) -> ChangeCase:
            return ChangeCase("", "", "", str(brief.get("goal") or ""), stopped=text)

        if "github" not in registry.adapters:
            return stop("GitHub is not connected for this organization, so I can't change code.")
        if "sandbox" not in registry.adapters:
            return stop("No sandbox is configured, so I can't change or test code safely.")
        notes: list[str] = []
        plan = await self.r._prior_plan(ref) or {}
        revision: dict[str, Any] | None = None
        pull = await self.r._session_pull(ref)
        if pull:
            got = await executor.run(
                tool_name("github", "get_pull_request"), {"repo": pull["repo"], "number": pull["number"]}
            )
            if got.denied or got.is_error:
                return stop(
                    f"I can't read {pull['repo']}#{pull['number']}: "
                    + got.text.removeprefix("Access denied: ")
                )
            if got.data.get("state") == "open":
                revision = got.data
            else:
                notes.append(
                    f"{pull['repo']}#{pull['number']} is {got.data.get('state')}: this is a new change"
                )
        if not plan and revision is None:
            return stop(
                "There's no plan in this thread to implement yet. Ask me to investigate the issue first, "
                "then to implement the plan."
            )
        repo = str(revision["repo"]) if revision else repository_of(plan)
        if not repo:
            return stop(
                "The plan doesn't name a repository or a file to change, so I don't know where to work."
            )
        args: dict[str, Any] = {"repo": repo, "push": True}
        if revision:
            args["base_sha"] = revision["head_sha"]
        prepared = await executor.run(tool_name("github", "prepare_workspace"), args)
        if prepared.denied or prepared.is_error:
            return stop(prepared.text.removeprefix("Access denied: "))
        base = str(prepared.data["base_sha"])
        ws = await Workspaces(self.r.d.pool, self._provider()).find(UUID(ref.org_id), UUID(ref.task_id), repo)
        recipe = await recipes.ensure(self.r.d.pool, ref.org_id, self._provider(), ws) if ws else None
        if recipe is None:
            notes.append("No test setup was detected for this repository")
        issue_key = ((plan.get("case") or {}).get("issue") or {}).get("key")
        goal = str(plan.get("goal") or brief.get("goal") or "the plan")
        if revision:
            goal = f"Address the feedback on {repo}#{revision['number']}: {revision.get('title') or goal}"
            branch = str(revision["head_ref"])
        else:
            branch = branch_name(issue_key, goal, ref.task_id)
        await self.r.step(ref, "gather", "completed", f"{repo} at {base[:7]} on {branch}")
        return ChangeCase(
            repo, base, branch, goal[:300], plan_for_model(plan), issue_key, revision, recipe,
            bool(prepared.data.get("private", True)), notes,
        )  # fmt: skip

    # ------------------------------------------------------------------ implement

    async def implement(
        self, ref: TaskRef, u: Understanding, case: ChangeCase, tier: str, feedback: dict[str, Any]
    ) -> Implemented:
        from app.agent.runner import playbook_of

        spec, _ = playbook_of(u)
        ctx = await self.r._context(ref)
        _, registry, _, executor = await self.r._tools(ref, ctx, u.mode, RefBook("W"))
        tools = [
            d
            for d in self.r._definitions(registry, spec, "read")
            if d["name"] in IMPLEMENTER_READS or d["name"].startswith("sandbox_")
        ]
        tools.append(prompts.submit_tool("submit_implementation", "Submit what you changed."))
        await self.r.step(ref, "act", "running", "Implementing the change")
        repository = {"repo": case.repo, "base": case.base_sha[:12], "test_command": case.recipe}
        content = f"<plan>{json.dumps(case.plan)}</plan>\n<repository>{json.dumps(repository)}</repository>"
        if case.revision:
            feedback_json = json.dumps(self._feedback(case.revision))
            content += f"\n<pull_request_feedback>{feedback_json}</pull_request_feedback>"
        if feedback.get("review"):
            content += f"\n<review>{json.dumps(feedback['review'])}</review>"
        if feedback.get("verification"):
            content += f"\n<verification>{json.dumps(feedback['verification'])}</verification>"
        if feedback.get("previous"):
            content += f"\n<previous_attempt>{json.dumps(feedback['previous'])}</previous_attempt>"
        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
        system = prompts.system(ctx.org_name, spec, ctx.roles, prompts.IMPLEMENT)
        denied: list[str] = []
        try:
            raw = await self.r._tool_loop(
                ref, tier, executor, tools, system, messages, "submit_implementation", "implement",
                denied, [],
                rounds=IMPLEMENT_ROUNDS, token_cap=IMPLEMENT_TOKENS, max_tokens=8000,
            )  # fmt: skip
        except BudgetExceededError as exc:
            return Implemented("", stopped=str(exc))
        raw = raw or {}
        summary = redact(" ".join(str(raw.get("summary") or "").split()))[0][:800]
        tests = [str(t)[:300] for t in raw.get("tests") or []][:10]
        notes = [redact(str(n))[0][:300] for n in raw.get("notes") or [] if str(n).strip()][:6]
        notes += [d[:300] for d in denied][:4]
        await self.r.step(ref, "act", "completed", f"Implemented: {summary[:200]}")
        return Implemented(summary, tests, notes)

    @staticmethod
    def _feedback(revision: dict[str, Any]) -> dict[str, Any]:
        return {
            "reviews": [r for r in revision.get("reviews") or [] if r.get("body")],
            "comments": revision.get("comments") or [],
            "failing_checks": [
                c for c in revision.get("checks") or [] if c.get("conclusion") in FAILED_STATES
            ],
        }

    # ------------------------------------------------------------------ verify

    async def verify(self, ref: TaskRef, u: Understanding, case: ChangeCase) -> Verification:
        await self.r.step(ref, "verify", "running", "Running the tests")
        if not case.recipe:
            result = Verification(
                False, False, None, "No test setup was detected, so the tests could not be run."
            )
            await self.r.step(ref, "verify", "completed", result.summary)
            return result
        provider = self._provider()
        ws = await self._workspace(ref, case)
        ran = await provider.exec(ws.sandbox_id, case.recipe, cwd=ws.root, seconds=TEST_SECONDS)
        status = await recipes.record(self.r.d.pool, ref.org_id, case.repo, case.recipe, ran, ref.task_id)
        output = tail(ran.stdout + "\n" + ran.stderr)
        if status == "unavailable":
            last = (output.splitlines() or [""])[-1][:200]
            result = Verification(False, False, case.recipe, f"The tests could not be run ({last}).", output,
                                  ran.exit_code)  # fmt: skip
        elif status == "failed":
            summary = "The tests timed out." if ran.timed_out else "The tests failed."
            result = Verification(False, True, case.recipe, summary, output, ran.exit_code)
        else:
            reproduced = await self._reproduced(provider, ws, case.recipe)
            result = Verification(True, True, case.recipe, "All tests pass.", output[-800:], 0, reproduced)
        await self.r.step(ref, "verify", "completed", result.summary)
        return result

    async def _reproduced(self, provider: SandboxProvider, ws: Workspace, command: str) -> bool | None:
        """Whether the change's tests fail without its other changes: the fix is stashed, the tests
        run, and the fix is put back. None when it cannot be checked (no new test, or only tests)."""
        change = await provider.diff(ws.sandbox_id, ws.root)
        tests = {f.path for f in change.files if TEST_FILE.search(f.path) and f.content is not None}
        others = [f.path for f in change.files if f.path not in tests]
        if not tests or not others:
            return None
        paths = " ".join(shlex.quote(p) for p in others)
        stashed = await provider.exec(
            ws.sandbox_id, f"git stash push --quiet --include-untracked -- {paths}", cwd=ws.root, seconds=60
        )
        if not stashed.ok:
            return None
        try:
            without = await provider.exec(ws.sandbox_id, command, cwd=ws.root, seconds=TEST_SECONDS)
        finally:
            back = await provider.exec(ws.sandbox_id, "git stash pop --quiet", cwd=ws.root, seconds=60)
            if not back.ok:
                raise SandboxError("the change could not be restored after the reproduction check")
        return recipes.outcome(without) == "failed"

    # ------------------------------------------------------------------ review

    async def review(self, ref: TaskRef, u: Understanding, case: ChangeCase, tier: str) -> ReviewResult:
        from app.agent.runner import as_tier, playbook_of

        spec, _ = playbook_of(u)
        ctx = await self.r._context(ref)
        ws = await self._workspace(ref, case)
        change = await self._provider().diff(ws.sandbox_id, root_of(case.repo))
        await self.r.step(ref, "verify", "running", "Reviewing the change")
        content = f"<plan>{json.dumps(case.plan)}</plan>\n<diff>\n{redact(change.patch[:40_000])[0]}\n</diff>"
        try:
            result = await self.r.d.gateway.complete(
                self.r._call(ref, "review"),
                as_tier(tier),
                system=prompts.system(ctx.org_name, spec, ctx.roles, prompts.REVIEW),
                messages=[{"role": "user", "content": content}],
                tools=[prompts.submit_tool("submit_review", "Submit the review.")],
                tool_choice={"type": "tool", "name": "submit_review"},
                max_tokens=2000,
            )
            raw = result.tool_input("submit_review")
        except BudgetExceededError as exc:
            return ReviewResult(False, f"Not reviewed: {exc}")
        summary = redact(" ".join(str(raw.get("summary") or "").split()))[0][:600]
        comments = []
        for c in (raw.get("comments") or [])[:10]:
            if isinstance(c, dict) and str(c.get("text") or "").strip():
                line = c.get("line") if isinstance(c.get("line"), int) else None
                comments.append({"path": str(c.get("path") or "")[:300], "line": line,
                                 "text": redact(str(c["text"]))[0][:500]})  # fmt: skip
        approved = raw.get("verdict") == "approve"
        if not approved and not comments:
            comments = [{"path": "", "line": None, "text": summary or "Changes requested."}]
        await self.r.step(
            ref, "verify", "completed", f"Review: {'approved' if approved else 'changes asked'}"
        )
        return ReviewResult(approved, summary, comments if not approved else [])

    # ------------------------------------------------------------------ export

    async def export(
        self,
        ref: TaskRef,
        u: Understanding,
        case: ChangeCase,
        verification: Verification,
        review: ReviewResult | None,
        implemented: Implemented,
    ) -> Exported:
        ctx = await self.r._context(ref)
        _, _, _, executor = await self.r._tools(ref, ctx, u.mode, RefBook())
        await self.r.step(ref, "act", "running", "Checking the change")
        got = await executor.run(tool_name("sandbox", "export_change"), {"repo": case.repo})
        if got.is_error:
            raise SandboxError(got.text)
        files = [
            {**f, "content": _b64(f["content_b64"]) if f.get("content_b64") is not None else None}
            for f in got.data.get("files") or []
        ]
        patch = str(got.data.get("patch") or "")
        repository_id, _, protected = await recipes.stored(self.r.d.pool, ref.org_id, case.repo)
        checks = check_change(files, patch, protected)
        if not verification.ran:
            checks.ask.append(verification.summary)
        if files and not checks.tests:
            checks.ask.append("The change adds or edits no test.")
        if verification.reproduced is False:
            checks.ask.append("The new test passes without the fix too, so it may not reproduce the problem.")
        if review is not None and not review.approved:
            checks.ask.append("The reviewer asked for changes; they were addressed but not reviewed again.")
        content = {
            "repo": case.repo,
            "base_sha": case.base_sha,
            "branch": case.branch,
            "patch": patch,
            "files": [
                {k: f[k] for k in ("path", "status", "mode", "content_b64")}
                for f in got.data.get("files") or []
            ],
            "checks": checks.to_dict(),
            "verification": asdict(verification),
        }
        artifact_id = await self._store(ref, content, checks.blocked)
        await Workspaces(self.r.d.pool, self._provider()).release_task(ref.org_id, ref.task_id)
        exported = Exported(
            artifact_id, [{"path": f["path"], "status": f["status"]} for f in files],
            checks.added, checks.removed,
            checks.blocked, checks.ask, checks.tests,
        )  # fmt: skip
        if not checks.blocked and artifact_id:
            exported.action = await self._push_action(ref, u, case, exported, verification, review,
                                                      implemented,
                                                      repository_id)  # fmt: skip
        summary = "; ".join(checks.blocked) or f"{len(files)} files, +{checks.added} -{checks.removed}"
        await self.r.step(ref, "act", "completed", f"Checked: {summary[:300]}")
        return exported

    async def _store(self, ref: TaskRef, content: dict[str, Any], blocked: list[str]) -> str | None:
        """The diff artifact: inline when small, in the object store when large."""
        body = json.dumps(content)
        storage_ref = None
        if len(body) > MAX_STORED:
            blocked.append("The change is too large to store; it should be split into smaller changes.")
            return None
        if len(body) > INLINE_LIMIT:
            if self.r.d.store is None:
                blocked.append("The change is too large to keep without object storage.")
                return None
            storage_ref = await self.r.d.store.put(
                UUID(ref.org_id), "diff", body.encode(), "application/json"
            )
        async with tenant_tx(self.r.d.pool, ref.org_id) as conn:
            row = await (
                await conn.execute(
                    "insert into app.artifacts (org_id, task_id, kind, content, storage_ref, source_refs) "
                    "values (%s, %s, 'diff', %s, %s, '[]') returning id",
                    (ref.org_id, ref.task_id, None if storage_ref else Jsonb(content), storage_ref),
                )
            ).fetchone()
        return str(row[0]) if row else None

    async def _push_action(
        self,
        ref: TaskRef,
        u: Understanding,
        case: ChangeCase,
        exported: Exported,
        verification: Verification,
        review: ReviewResult | None,
        implemented: Implemented,
        repository_id: Any,
    ) -> PlannedAction:
        number = case.revision["number"] if case.revision else None
        title = (
            f"{case.issue_key}: {case.plan.get('goal') or case.goal}" if case.issue_key and not number
            else f"Address review feedback (#{number})" if number else str(case.plan.get("goal") or case.goal)
        )[:120]  # fmt: skip
        link = await self._thread_link(ref)
        body = pr_body(case, exported, verification, review, implemented, link)
        files = ", ".join(f["path"] for f in exported.files[:8]) + (" …" if len(exported.files) > 8 else "")
        summary = (
            f"Push a revision to {case.repo}#{number} ({case.branch})" if number
            else f"Push the change to {case.branch} and open a pull request in {case.repo}"
        )  # fmt: skip
        evidence = [f"{len(exported.files)} files (+{exported.added} -{exported.removed}): {files}",
                    f"Verified: {verification.summary}",
                    *(f"Needs a person: {a}" for a in exported.ask)]  # fmt: skip
        args = {"repo": case.repo, "change": exported.artifact_id, "base_sha": case.base_sha,
                "branch": case.branch,
                "title": title, "body": body, "summary": implemented.summary}  # fmt: skip
        if number:
            args["pull_number"] = number
        ask = bool(exported.ask) or await self._repo_asks(ref, repository_id)
        return PlannedAction("github", "push_changes", args, escape(summary), evidence, ask=ask)

    async def _repo_asks(self, ref: TaskRef, repository_id: Any) -> bool:
        """The repository's own autonomy ceiling: Auto only when it (and the requester) allow it."""
        if repository_id is None:
            return False
        async with tenant_tx(self.r.d.pool, ref.org_id) as conn:
            row = await (
                await conn.execute(
                    "select app.effective_mode(%s, %s, %s)::text",
                    (ref.membership_id, ref.channel_id, repository_id),
                )
            ).fetchone()
        return row is not None and row[0] != "auto"

    async def _thread_link(self, ref: TaskRef) -> str | None:
        team, token = await self.r._token(ref)
        try:
            body = await self.r.d.slack.call(
                "chat.getPermalink", token, team, channel=ref.channel, message_ts=ref.message_ts
            )
        except SlackError:
            return None
        link = body.get("permalink")
        return str(link) if link else None

    # ------------------------------------------------------------------ report

    async def report(
        self,
        ref: TaskRef,
        u: Understanding,
        case: ChangeCase,
        attempts: list[Implemented],
        verification: Verification | None,
        review: ReviewResult | None,
        exported: Exported | None,
        outcome: ActionOutcome | None,
    ) -> Composed:
        """What happened, written in code from the steps' results (model text escaped)."""
        number = case.revision["number"] if case.revision else None
        pr_url = (outcome.result.get("url") if outcome and outcome.status == "done" else None) or (
            case.revision.get("url") if case.revision else None
        )
        lines: list[str] = []
        not_verified: list[str] = []
        if outcome is not None and outcome.status == "done":
            label = f"{case.repo}#{outcome.result.get('number') or number}"
            verb = "Pushed a revision to" if number else "Opened"
            lines.append(f"*{verb} <{pr_url}|{escape(label)}>* on `{escape(case.branch)}`.")
        elif exported is None:
            tries = len(attempts)
            lines.append(
                f"I stopped: the tests still fail after {tries} attempts, so I didn't push anything."
            )
        elif exported.blocked:
            lines.append("*Not pushed*: the change failed checks that block a push.")
            lines += [f"• {escape(b)}" for b in exported.blocked]
        elif outcome is not None:
            lines.append(f"*Not pushed*: {escape(outcome.message)}")
        if attempts and attempts[-1].summary:
            lines.append(escape(attempts[-1].summary))
        if exported is not None and exported.files:
            shown = ", ".join(f"`{escape(f['path'])}` ({f['status']})" for f in exported.files[:10])
            lines.append(
                f"*Changed* {len(exported.files)} files (+{exported.added} -{exported.removed}): {shown}"
            )
        if verification is not None:
            text = f"*Tests*: {escape(verification.summary)}"
            if verification.command:
                text += f" (`{escape(verification.command)}`)"
            if verification.reproduced:
                text += " The new test fails without the fix and passes with it."
            lines.append(text)
            if not verification.ran:
                not_verified.append("The tests could not be run")
            elif verification.reproduced is False:
                not_verified.append("The new test passes without the fix")
            elif verification.reproduced is None and verification.ok:
                not_verified.append("No test was checked to fail without the fix")
            if exported is None and verification.output:
                lines.append(
                    "Last test output:\n```" + verification.output[-1500:].replace("```", "'''") + "```"
                )
                lines.append(
                    "The plan may need revising: tell me what you know, or ask me to investigate again."
                )
        if review is not None:
            lines.append(
                "*Review*: approved." if review.approved
                else "*Review*: changes asked: " + "; ".join(escape(c["text"]) for c in review.comments[:3])
                + (" They were addressed in a second round." if len(attempts) > 1 else "")
            )  # fmt: skip
        if exported is not None and exported.ask and not exported.blocked:
            lines.append("*Needed a person's approval*: " + "; ".join(escape(a) for a in exported.ask))
        for a in attempts:
            not_verified += [escape(n) for n in a.notes][:3]
        sources = [self._source(case, pr_url)]
        return Composed("\n".join(lines), sources, "high" if verification and verification.ok else "medium",
                        1 if verification and verification.ok else 0, 1, not_verified[:6])  # fmt: skip

    @staticmethod
    def _source(case: ChangeCase, url: str | None) -> dict[str, Any]:
        return Source(
            ref=f"github:{case.repo}",
            channel_external_id="",
            channel_name=case.repo,
            visibility="restricted" if case.private else "public",
            ts="",
            label=case.repo,
            permalink=url or f"https://github.com/{case.repo}",
            kind="github",
            audience=f"github:{case.repo}",
        ).to_dict()


def pr_body(
    case: ChangeCase,
    exported: Exported,
    verification: Verification,
    review: ReviewResult | None,
    implemented: Implemented,
    thread: str | None,
) -> str:
    """The pull request's description (GitHub markdown): the plan, what changed, what was verified and
    what was not, and links back to the conversation and the issue."""
    plan = case.plan
    parts = [f"## {plan.get('goal') or case.goal}"]
    if plan.get("summary"):
        parts.append(str(plan["summary"]))
    if implemented.summary:
        parts.append(f"### What changed\n{implemented.summary}")
    steps = plan.get("steps") or []
    if steps:
        parts.append("### Plan\n" + "\n".join(f"{n}. {s.get('text')}" for n, s in enumerate(steps, 1)))
    checked = [
        f"- Tests: {verification.summary}" + (f" (`{verification.command}`)" if verification.command else "")
    ]
    if verification.reproduced:
        checked.append("- The new test fails without the fix and passes with it.")
    if review is not None:
        checked.append(
            "- Reviewed by a second agent: " + ("approved" if review.approved else "changes addressed")
        )
    parts.append("### Verified\n" + "\n".join(checked))
    unverified = [*exported.ask]
    if plan.get("verification"):
        unverified.append(f"After merging: {plan['verification']}")
    if unverified:
        parts.append("### Not verified\n" + "\n".join(f"- {x}" for x in unverified))
    links = []
    if thread:
        links.append(f"[Slack thread]({thread})")
    if case.issue_key:
        links.append(f"Issue {case.issue_key}")
    if links:
        parts.append("### Context\n" + " · ".join(links))
    parts.append("_Opened by the agent on behalf of the person who asked; a person reviews and merges._")
    return "\n\n".join(parts)[:60000]


def _b64(value: str) -> bytes:
    return base64.b64decode(value)

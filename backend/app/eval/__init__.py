"""The eval harness (spec 15): replay cases through the agent and score them.

``app eval`` runs every case in ``app/eval/cases`` through the same runner steps the task
workflow uses (understand, gather, plan, compose, deliver), with a capturing output sink so
nothing is posted, against the configured database, Slack and model. Locally that is the seeded
fixture, the Slack stand-in and the Anthropic stand-in; on staging it is the real model.

A case may seed messages first (only possible against the Slack stand-in; such cases are skipped
against real Slack), set up confirmed catalog services, and require integrations (skipped when the
org has not connected them). Ticket cases score the plan after the checks in code. Scores per
check are 0 to 1; a case scores their mean. The command prints a table, writes a JSON report and
exits non zero when the mean is under ``--min-score``.
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.agent.outbox import CaptureSink, Sink
from app.agent.runner import AgentDeps, AgentRunner, render_answer
from app.db.tenant import tenant_tx
from app.settings import Settings
from app.workflows.slack_activities import SlackActivities
from app.workflows.slack_types import SlackRequest

log = logging.getLogger(__name__)
CASES = Path(__file__).with_name("cases")


@dataclass
class CaseResult:
    id: str
    score: float
    checks: dict[str, float]
    tokens: int
    seconds: float
    skipped: str | None = None
    notes: list[str] = field(default_factory=list)


def skip(case: dict[str, Any], reason: str) -> CaseResult:
    return CaseResult(case["id"], 0.0, {}, 0, 0.0, skipped=reason)


def load_cases(directory: Path = CASES) -> list[dict[str, Any]]:
    cases = []
    for path in sorted(directory.glob("*.json")):
        case = json.loads(path.read_text())
        missing = {"id", "request", "requester", "channel", "expect"} - case.keys()
        if missing:
            raise ValueError(f"{path.name}: missing {sorted(missing)}")
        cases.append(case)
    return cases


def score(case: dict[str, Any], observed: dict[str, Any]) -> dict[str, float]:
    """One score per expectation the case states."""
    expect = case["expect"]
    answer = str(observed.get("answer") or "").lower()
    checks: dict[str, float] = {}
    if "playbook" in expect:
        checks["playbook"] = float(observed.get("playbook") == expect["playbook"])
    if "channels" in expect:
        checks["brief_channels"] = float(sorted(observed.get("channels") or []) == sorted(expect["channels"]))
    if "window" in expect:
        checks["brief_window"] = float(observed.get("window") == expect["window"])
    if expect.get("answer_includes"):
        hits = [s for s in expect["answer_includes"] if s.lower() in answer]
        checks["answer_includes"] = len(hits) / len(expect["answer_includes"])
    if expect.get("answer_excludes"):
        leaked = any(s.lower() in answer for s in expect["answer_excludes"])
        checks["answer_excludes"] = float(not leaked)
    if expect.get("cites_setup"):
        wanted = set(observed.get("setup_ts") or [])
        cited = set(observed.get("cited_ts") or [])
        checks["retrieval_recall"] = len(wanted & cited) / len(wanted) if wanted else 0.0
    if "refused" in expect:
        checks["refusal"] = float(bool(observed.get("refused")) == bool(expect["refused"]))
    if "delivered" in expect:
        checks["delivery"] = float(observed.get("delivered") == expect["delivered"])
    if expect.get("tickets"):  # proposed tickets kept by the checks in code
        kept = [
            a for a in observed.get("actions") or [] if a["operation"] == "create_issue" and not a["skip"]
        ]

        def found(want: dict[str, Any]) -> bool:
            return any(
                want["summary_includes"].lower() in str(a["args"].get("summary", "")).lower()
                and ("assignee" not in want or a["args"].get("assignee_email") == want["assignee"])
                for a in kept
            )

        checks["tickets"] = sum(found(w) for w in expect["tickets"]) / len(expect["tickets"])
    if expect.get("skips_include"):  # what the checks in code dropped, and why
        skips = " ".join(str(a["skip"]) for a in observed.get("actions") or [] if a["skip"])
        checks["skips"] = sum(s in skips for s in expect["skips_include"]) / len(expect["skips_include"])
    if "max_tokens" in case:
        checks["token_budget"] = float(observed.get("tokens", 0) <= case["max_tokens"])
    return checks


def _fill(value: Any, tag: str) -> Any:
    if isinstance(value, str):
        return value.replace("{tag}", tag)
    if isinstance(value, list):
        return [_fill(v, tag) for v in value]
    return value


@dataclass
class EvalRun:
    runner: AgentRunner
    intake: SlackActivities
    pool: Any
    slack_ui: str | None  # the stand-in's UI endpoints, when seeding messages is possible

    async def post(self, team: str, user: str, channel: str, text: str, thread: str | None = None) -> str:
        assert self.slack_ui is not None
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{self.slack_ui}/_ui/post",
                json={"team": team, "user": user, "channel": channel, "text": text, "thread_ts": thread},
            )
        response.raise_for_status()
        return str(response.json()["message"]["ts"])

    async def workspace(self, team: str) -> tuple[str, str] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "select org_id, workspace_id from app.resolve_workspace('slack', %s)", (team,)
            )
            row = await cur.fetchone()
        return (str(row[0]), str(row[1])) if row else None

    async def missing(self, org_id: str, integrations: list[str]) -> list[str]:
        """Integrations a case needs that the org has not enabled and connected (the harness cannot
        connect them: an admin does, as in the end to end tests)."""
        if not integrations:
            return []
        async with tenant_tx(self.pool, org_id) as conn:
            rows = await (
                await conn.execute(
                    "select i.integration_key from app.org_integrations i join app.connections c "
                    "on c.integration_key = i.integration_key and c.scope = 'org' and c.status = 'active' "
                    "where i.enabled and i.integration_key = any(%s)",
                    (integrations,),
                )
            ).fetchall()
        have = {r[0] for r in rows}
        return [k for k in integrations if k not in have]

    async def catalog(self, org_id: str, services: list[dict[str, Any]]) -> None:
        """Confirmed services a case relies on (kept when they exist already)."""
        async with tenant_tx(self.pool, org_id) as conn:
            for s in services:
                await conn.execute(
                    "insert into app.services (org_id, name, jira_project, jira_component) "
                    "values (%s, %s, %s, %s) on conflict do nothing",
                    (org_id, s["name"], s.get("jira_project"), s.get("jira_component")),
                )
                await conn.execute(
                    "insert into app.service_owners (org_id, service_id, membership_id, source, reason) "
                    "select %s, s.id, m.id, 'manual', %s from app.services s, app.org_memberships m "
                    "join app.users u on u.id = m.user_id where lower(btrim(s.name)) = lower(btrim(%s)) "
                    "and s.status = 'active' and u.email = %s on conflict do nothing",
                    (org_id, s.get("reason"), s["name"], s["owner"]),
                )

    async def run_case(self, case: dict[str, Any]) -> CaseResult:
        started = time.monotonic()
        team = case.get("team", "T0ACME")
        tag = uuid.uuid4().hex[:6]
        if case.get("setup") and self.slack_ui is None:
            return skip(case, "needs the Slack stand-in to seed messages")
        found = await self.workspace(team)
        if found is None:
            return skip(case, f"workspace {team} is not connected")
        missing = await self.missing(found[0], case.get("requires") or [])
        if missing:
            return skip(case, f"needs {', '.join(missing)} connected for the org")
        await self.catalog(found[0], case.get("catalog") or [])
        setup_ts: list[str] = []
        for message in case.get("setup") or []:
            parent = setup_ts[message["reply_to"]] if "reply_to" in message else None
            text = _fill(message["text"], tag)
            setup_ts.append(await self.post(team, message["user"], message["channel"], text, parent))
        thread = setup_ts[case["in_thread_of"]] if "in_thread_of" in case else None
        ts = await self.post(team, case["requester"], case["channel"], _fill(case["request"], tag), thread)
        req = SlackRequest(
            found[0], found[1], team, case["channel"], case.get("channel_type", "channel"),
            case["requester"], ts, thread, f"eval-{uuid.uuid4().hex[:12]}", "app_mention",
        )  # fmt: skip
        requester = await self.intake.identify_requester(req)
        if requester.membership_id is None:
            return skip(case, f"{case['requester']} is not an org member")
        ref = await self.intake.create_task(req, requester)
        if ref is None:
            return skip(case, "the agent runtime is not configured")
        observed = await self.observe(ref, setup_ts)
        checks = score({**case, "expect": _fill(case["expect"], tag)}, observed)
        mean = sum(checks.values()) / len(checks) if checks else 0.0
        return CaseResult(
            case["id"], round(mean, 3), checks, int(observed.get("tokens", 0)),
            round(time.monotonic() - started, 2), notes=[k for k, v in checks.items() if v < 1],
        )  # fmt: skip

    async def observe(self, ref: Any, setup_ts: list[str]) -> dict[str, Any]:
        observed: dict[str, Any] = {"setup_ts": setup_ts}
        try:
            start = await self.runner.start(ref)
            if start.status != "started":
                observed["answer"] = start.message
                return observed
            u = await self.runner.understand(ref, start.mode)
            observed["playbook"] = u.playbook_key or "none"
            if u.brief:
                observed["channels"] = u.brief["entities"]["channels"]
                observed["window"] = u.brief["entities"]["time_window"]["label"]
            if u.playbook_key and not u.stopped:
                f = await self.runner.gather(ref, u, start.model_tier)
                observed["refused"] = any("not a member" in g or "not enabled" in g for g in f.gaps)
                actions = await self.runner.plan(ref, u, f, start.model_tier)
                observed["actions"] = [
                    {"operation": a.operation, "summary": a.summary, "args": a.args, "skip": a.skip}
                    for a in actions
                ]
                c = await self.runner.compose(ref, u, f, start.model_tier)
                observed["answer"] = render_answer(c, [])
                observed["cited_ts"] = [s["ts"] for s in c.sources]
                observed["delivered"] = (await self.runner.deliver(ref, u, c, [])).where
            else:
                observed["answer"] = u.question or u.stopped or ""
            observed["tokens"] = (await self.runner.usage(ref))[0]
        finally:
            await self.runner.finish(ref, "completed")
        return observed


async def evaluate(settings: Settings, cases: list[dict[str, Any]]) -> list[CaseResult]:
    from app import kv
    from app.db.pool import open_pool
    from app.llm.gateway import gateway_from_settings
    from app.slack.client import SlackClient
    from app.slack.limiter import SlackRateLimiter
    from app.vault_service.factory import vault_from_settings
    from app.workflows.slack_activities import SlackDeps

    closers: list[Any] = []
    vault = await vault_from_settings(settings, closers)
    if vault is None:
        raise RuntimeError("no vault configured (VAULT_URL, or the vault database user and ENCRYPTION_KEYS)")
    pool = await open_pool(settings.component_dsn("worker"), "worker", min_size=1, max_size=4)
    kv_client: Any = kv.make_client(settings.redis_url.get_secret_value()) if settings.redis_url else None
    limiter = SlackRateLimiter(kv_client) if kv_client is not None else None
    slack = SlackClient(settings.slack_api_url, limiter=limiter)
    gateway = gateway_from_settings(settings, pool, kv_client)
    if gateway is None:
        raise RuntimeError("ANTHROPIC_API_KEY is required")
    sink = CaptureSink()

    def capture(team: str, token: str) -> Sink:
        return sink

    http = httpx.AsyncClient(timeout=30.0)
    closers.append(http.aclose)
    deps = AgentDeps(
        pool,
        vault,
        slack,
        kv_client,
        gateway,
        settings.console_url,
        24,
        capture,
        settings=settings,
        http=http,
    )
    intake = SlackActivities(
        SlackDeps(pool, vault, slack, kv_client, settings.console_url, agent_enabled=True)
    )
    ui = settings.slack_api_url.removesuffix("/api") if "/slack/api" in settings.slack_api_url else None
    run = EvalRun(AgentRunner(deps), intake, pool, ui)
    results = []
    try:
        for case in cases:
            try:
                results.append(await run.run_case(case))
            except Exception as exc:  # a broken case scores 0 and the run goes on
                log.exception("eval case %s failed", case["id"])
                results.append(
                    CaseResult(case["id"], 0.0, {}, 0, 0.0, notes=[f"error: {type(exc).__name__}"])
                )
    finally:
        await gateway.aclose()
        await slack.aclose()
        await pool.close()
        for close in closers:
            await close()
        if kv_client is not None:
            await kv_client.aclose()
    return results


def summarize(results: list[CaseResult]) -> tuple[str, float]:
    scored = [r for r in results if r.skipped is None]
    mean = sum(r.score for r in scored) / len(scored) if scored else 0.0
    lines = [f"{'case':36} {'score':>6} {'tokens':>8} {'secs':>6}  notes"]
    for r in results:
        if r.skipped:
            lines.append(f"{r.id:36} {'skip':>6} {'':>8} {'':>6}  {r.skipped}")
            continue
        notes = ", ".join(r.notes) or "ok"
        lines.append(f"{r.id:36} {r.score:6.2f} {r.tokens:8d} {r.seconds:6.1f}  {notes}")
    lines.append(f"{'mean':36} {mean:6.2f}  ({len(scored)} scored, {len(results) - len(scored)} skipped)")
    return "\n".join(lines), mean


def run_eval(settings: Settings, cases_dir: Path, report: Path | None, min_score: float) -> int:
    cases = load_cases(cases_dir)
    results = asyncio.run(evaluate(settings, cases))
    table, mean = summarize(results)
    print(table)
    if report is not None:
        report.write_text(json.dumps({"mean": mean, "results": [asdict(r) for r in results]}, indent=2))
    return 0 if mean >= min_score else 1

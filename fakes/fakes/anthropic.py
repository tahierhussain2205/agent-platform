"""Anthropic Messages API stand-in.

``POST /anthropic/v1/messages`` with Anthropic's request and response shapes: ``x-api-key`` auth,
``system`` blocks with ``cache_control``, ``tools``/``tool_choice``, ``tool_use``/``tool_result``
blocks, ``stop_reason``, ``usage`` (with cache creation and read tokens) and Anthropic's error
envelope.

It is a small deterministic "model" for the agent's steps, recognised by the forced tool name
(``submit_classification``, ``submit_brief``, ``submit_findings``, ``submit_plan``,
``submit_report``) or by the Slack read tools on offer. Answers come from simple heuristics over
the request (channel names, time words, "share in #x") and over tool results (``[S1]`` lines).
Real quality is measured with the eval suite against the real model (blocker B-005).

Test hooks under ``/_admin/anthropic``: fault injection, scripted responses (to play a model that
was persuaded by injected instructions) and the recorded requests.
"""

import hashlib
import json
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from fakes.config import Config

CACHE_TTL = 300.0
CHANNEL = re.compile(r"(?<![\w&])#([a-z0-9][a-z0-9_-]{0,79})")
# Our adapters' line format: "[S1] (2026-10-16 09:01 UTC) Quinn Tester: text", replies indented with "↳".
LINE = re.compile(r"^\s*(?:↳\s*)?\[([A-Z]\d+)\]\s+(?:\([^)]*\)\s+)?([^:]{1,80}):\s?(.*)$")
TAG = "<{0}>(.*?)</{0}>"


def tag(text: str, name: str) -> str | None:
    match = re.search(TAG.format(name), text, re.DOTALL)
    return match.group(1).strip() if match else None


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif block.get("type") == "tool_result":
            parts.append(text_of(block.get("content")))
    return "\n".join(parts)


def tokens(value: Any) -> int:
    return max(1, len(value if isinstance(value, str) else json.dumps(value)) // 4)


@dataclass
class Fault:
    status: int
    count: int
    retry_after: float | None = None


@dataclass
class AnthropicState:
    faults: deque[Fault] = field(default_factory=deque)
    scripts: deque[dict[str, Any]] = field(default_factory=deque)
    requests: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=200))
    cache: dict[str, float] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


class Anthropic:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.state = AnthropicState()

    def reset(self) -> None:
        self.state = AnthropicState()

    # ------------------------------------------------------------------ request handling

    def handle(self, body: dict[str, Any], api_key: str | None) -> tuple[int, dict[str, Any], dict[str, str]]:
        if api_key != self.config.anthropic_api_key:
            return 401, error("authentication_error", "invalid x-api-key"), {}
        with self.state.lock:
            self.state.requests.append(body)
            if self.state.faults:
                fault = self.state.faults[0]
                fault.count -= 1
                if fault.count <= 0:
                    self.state.faults.popleft()
                headers = {"retry-after": str(fault.retry_after)} if fault.retry_after is not None else {}
                kind = {429: "rate_limit_error", 529: "overloaded_error"}.get(fault.status, "api_error")
                return fault.status, error(kind, f"injected {fault.status}"), headers
            script = self.state.scripts.popleft() if self.state.scripts else None
        model = body.get("model")
        if model not in self.config.anthropic_models:
            return 404, error("not_found_error", f"model: {model}"), {}
        if not isinstance(body.get("messages"), list) or not body["messages"]:
            return 400, error("invalid_request_error", "messages: field required"), {}
        if not isinstance(body.get("max_tokens"), int) or body["max_tokens"] < 1:
            return 400, error("invalid_request_error", "max_tokens: field required"), {}
        content = script["content"] if script else self.respond(body)
        return 200, self.envelope(body, content), {}

    def envelope(self, body: dict[str, Any], content: list[dict[str, Any]]) -> dict[str, Any]:
        for block in content:
            if block.get("type") == "tool_use":
                block.setdefault("id", f"toolu_{uuid.uuid4().hex[:20]}")
        cached, created = self.cache_usage(body)
        total_in = tokens({k: body.get(k) for k in ("system", "messages", "tools")})
        return {
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "model": body["model"],
            "content": content,
            "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": max(1, total_in - cached - created),
                "output_tokens": tokens(content),
                "cache_creation_input_tokens": created,
                "cache_read_input_tokens": cached,
            },
        }

    def cache_usage(self, body: dict[str, Any]) -> tuple[int, int]:
        """Prompt caching: the prefix up to the last ``cache_control`` breakpoint (tools, then
        system) is a cache hit when the same prefix was sent within five minutes."""
        prefix: list[Any] = []
        cut = 0
        for block in [*(body.get("tools") or []), *(body.get("system") or [])]:
            if not isinstance(block, dict):
                continue
            prefix.append(block)
            if block.get("cache_control"):
                cut = len(prefix)
        if not cut:
            return 0, 0
        stable = prefix[:cut]
        digest = hashlib.sha256(json.dumps([body["model"], stable], sort_keys=True).encode()).hexdigest()
        now = time.monotonic()
        with self.state.lock:
            hit = self.state.cache.get(digest, 0) > now
            self.state.cache[digest] = now + CACHE_TTL
        size = tokens(stable)
        return (size, 0) if hit else (0, size)

    # ------------------------------------------------------------------ the "model"

    def respond(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        forced = (body.get("tool_choice") or {}).get("name")
        last_user = text_of(body["messages"][-1].get("content")) if body["messages"] else ""
        first_user = text_of(body["messages"][0].get("content"))
        tool_names = {t.get("name") for t in body.get("tools") or []}
        if forced == "submit_classification":
            return [use(forced, classify(last_user))]
        if forced == "submit_brief":
            return [use(forced, brief(last_user))]
        if forced == "submit_plan":
            return [use(forced, plan(last_user))]
        if forced == "submit_report":
            return [use(forced, report(last_user))]
        if forced == "submit_report_narrative":
            return [use(forced, narrative(last_user))]
        if forced == "submit_schedule":
            return [use(forced, schedule(last_user))]
        if "submit_findings" in tool_names:
            return gather(body["messages"], first_user, {str(t) for t in tool_names})
        return [{"type": "text", "text": "OK."}]


def use(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "name": name, "input": payload}


def error(kind: str, message: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": message}}


QUESTION = re.compile(
    r"\?|\b(summar\w*|recap|catch me up|what|why|who|when|where|how|status|list|tell me|share|post|any)\b",
    re.IGNORECASE,
)


RECURRING = re.compile(r"\b(every|daily|weekly|hourly|each (day|morning|week))\b", re.IGNORECASE)
DAY_NUMBERS = {"sunday": 0, "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4, "friday": 5, "saturday": 6}
NOT_PROJECTS = {"I", "AM", "PM", "DM", "UTC", "OK", "PR"}


def schedule(user: str) -> dict[str, Any]:
    """A proposed schedule from phrases like "every weekday at 9am send me a summary of #bugs" or
    "every Monday at 10:30 post the PAY Jira progress in #general"."""
    request = re.sub(r"@\S+", "", tag(user, "request") or "").strip()
    lowered = request.lower()
    at = re.search(r"\bat (\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", lowered)
    hour, minute = (int(at[1]), int(at[2] or 0)) if at else (9, 0)
    if at and at[3] == "pm" and hour < 12:
        hour += 12
    if at and at[3] == "am" and hour == 12:
        hour = 0
    days = [str(n) for name, n in DAY_NUMBERS.items() if re.search(rf"\b{name}s?\b", lowered)]
    if "hourly" in lowered or "every hour" in lowered:
        cron, cadence = f"{minute} * * * *", "Hourly"
    elif "weekday" in lowered:
        cron, cadence = f"{minute} {hour} * * 1-5", "Weekday"
    elif days:
        cron, cadence = f"{minute} {hour} * * {','.join(sorted(days))}", "Weekly"
    elif "weekly" in lowered or "every week" in lowered:
        cron, cadence = f"{minute} {hour} * * 1", "Weekly"
    else:
        cron, cadence = f"{minute} {hour} * * *", "Daily"
    target = re.search(r"\b(?:post|share|deliver|send)\b[^#]*?\b(?:in|to|into)\s+#([a-z0-9][a-z0-9_-]*)", lowered)
    delivery_channel = target[1] if target else None
    channels = [c for c in CHANNEL.findall(lowered) if c != delivery_channel]
    projects = [p for p in PROJECT.findall(request) if p not in NOT_PROJECTS]
    jira = bool(projects) and bool(re.search(r"\b(jira|progress|issues|tickets)\b", lowered))
    result: dict[str, Any] = {
        "cron": cron,
        "delivery": "channel" if delivery_channel else "dm",
        "delivery_channel": delivery_channel,
        "channels": [] if jira else channels[:5],
        "project": projects[0] if jira else None,
        "kind": "jira_progress" if jira else "channel_summary",
        "question": None,
    }
    if jira:
        result["name"] = f"{cadence} {projects[0]} progress"
    elif channels:
        result["name"] = f"{cadence} " + ", ".join(f"#{c}" for c in channels[:5]) + " summary"
    else:
        result["name"] = f"{cadence} report"
        result["question"] = "Which channels should the report summarize, or which Jira project?"
    return result


def classify(user: str) -> dict[str, Any]:
    request = tag(user, "request") or user
    words = re.sub(r"@\S+", "", request).strip()
    if RECURRING.search(words) and re.search(r"\b(report|summar\w*|progress|send|post)\b", words, re.IGNORECASE):
        return {"playbook": "schedule_create", "confidence": 0.9, "clarifying_question": None}
    if not words or not QUESTION.search(words):
        return {
            "playbook": "none",
            "confidence": 0.3,
            "clarifying_question": "What would you like me to do? For example: summarize today's discussion in #bugs.",
        }
    unsure = re.search(r"\b(maybe|not sure|perhaps)\b", words, re.IGNORECASE)
    return {"playbook": "slack_qa", "confidence": 0.5 if unsure else 0.92, "clarifying_question": None}


def window(text: str, now: datetime) -> dict[str, Any]:
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    lowered = text.lower()
    if "yesterday" in lowered:
        start, end, label = midnight - timedelta(days=1), midnight, "yesterday"
    elif "this week" in lowered:
        start, end, label = midnight - timedelta(days=now.weekday()), now, "this week"
    elif m := re.search(r"last (\d{1,3}) hours?", lowered):
        start, end, label = now - timedelta(hours=int(m.group(1))), now, f"last {m.group(1)} hours"
    elif "today" in lowered:
        start, end, label = midnight, now, "today"
    else:
        return {"start": None, "end": None, "label": "any time"}
    return {"start": start.isoformat(), "end": end.isoformat(), "label": label}


def brief(user: str) -> dict[str, Any]:
    thread = tag(user, "thread") or ""
    ref = tag(user, "request_ref") or ""
    now = datetime.fromisoformat(tag(user, "now") or datetime.now(UTC).isoformat())
    request_line = next((line for line in thread.splitlines() if line.startswith(f"[{ref}]")), "")
    parsed = LINE.match(request_line)
    request = parsed.group(3) if parsed else request_line
    asker = parsed.group(2) if parsed else "the requester"
    goal = re.sub(r"@\S+\s*", "", request).strip() or request
    return {
        "goal": goal,
        "facts": [{"text": f"{asker} asked: {goal}", "sources": [ref] if ref else []}],
        "constraints": [],
        "decisions": [],
        "open_questions": [],
        "entities": {
            # A channel to share in is a destination, not a source.
            "channels": sorted(set(CHANNEL.findall(request)) - set(SHARE.findall(request))),
            "people": [asker],
            "time_window": window(request, now),
            "ids": re.findall(r"\b[A-Z]{2,10}-\d+\b", request),
            "repositories": REPO.findall(request),
            "files": [f for f in FILE.findall(request) if "/" in f or "." in f or f == "CODEOWNERS"],
        },
    }


# owner/name, not part of a longer path (src/pricing/cart.py is a file, not a repository)
REPO = re.compile(r"(?<![\w/.-])([a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9_-]+)?)(?![\w/.-])")
FILE = re.compile(r"\b(CODEOWNERS|README(?:\.md)?|[\w.-]+/[\w./-]+\.\w+|[\w-]+\.(?:py|ts|md|tf|yaml|json))\b")
PROJECT = re.compile(r"\b([A-Z]{2,10})\b(?!-\d)")


def tool_calls(tools: set[str], the_brief: dict[str, Any]) -> list[dict[str, Any]]:
    """Calls to Jira, GitHub and MCP tools the brief asks for (when those tools are offered)."""
    entities = the_brief.get("entities") or {}
    goal = str(the_brief.get("goal") or "")
    lowered = goal.lower()
    calls = []
    for key in entities.get("ids") or []:
        if "jira_get_issue" in tools:
            calls.append(use("jira_get_issue", {"key": key}))
    words = re.findall(r"(?<![\w#-])(jira|tickets?|issues?|bugs?)(?![\w-])", lowered)  # not #bugs
    if not entities.get("ids") and words and "jira_search_issues" in tools:
        projects = [p for p in PROJECT.findall(goal) if p not in ("I", "OK", "UTC")]
        calls.append(use("jira_search_issues", {"project": projects[0]} if projects else {"jql": ""}))
    for repo in entities.get("repositories") or []:
        files = [f for f in entities.get("files") or [] if f != "CODEOWNERS"]
        owners = re.search(r"\bown(s|ed|er|ers|ership)?\b", lowered) or "CODEOWNERS" in (entities.get("files") or [])
        if owners and "github_get_codeowners" in tools:
            calls.append(use("github_get_codeowners", {"repo": repo}))
        for path in files:
            if "github_read_file" in tools:
                calls.append(use("github_read_file", {"repo": repo, "path": path}))
    if (
        not entities.get("repositories")
        and re.search(r"\brepo(sitorie)?s?\b", lowered)
        and "github_list_repos" in tools
    ):
        calls.append(use("github_list_repos", {}))
    sheet = re.search(r"\b([a-z0-9]+(?:-[a-z0-9]+)+)\b", goal)
    reader = next((t for t in tools if t.endswith("_sheets_read_range")), None)
    if reader and "sheet" in lowered and sheet:
        calls.append(use(reader, {"spreadsheet_id": sheet.group(1)}))
    return calls


def gather(messages: list[dict[str, Any]], first_user: str, tools: set[str] | None = None) -> list[dict[str, Any]]:
    """Read every channel the brief names (or the current thread) and call the Jira, GitHub and MCP
    tools it asks for, then submit findings built from the lines of the tool results."""
    try:
        the_brief = json.loads(tag(first_user, "brief") or "{}")
    except ValueError:
        the_brief = {}
    entities = the_brief.get("entities") or {}
    channels = entities.get("channels") or []
    tw = entities.get("time_window") or {}
    asked: set[str] = set()
    results: list[str] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                asked.add(json.dumps(block.get("input"), sort_keys=True))
            elif block.get("type") == "tool_result":
                results.append(text_of(block.get("content")))
    calls = [c for c in tool_calls(tools or set(), the_brief) if json.dumps(c["input"], sort_keys=True) not in asked]
    other_tools = bool(tool_calls(tools or set(), the_brief))
    if channels:
        for name in channels:
            args = {"channel": name, **{k: tw[k] for k in ("start", "end") if tw.get(k)}}
            if json.dumps(args, sort_keys=True) not in asked:
                calls.append(use("slack_read_channel", args))
    elif not other_tools:
        conversation = re.search(r'<conversation channel="([^"]*)" thread="([^"]*)"', first_user)
        if conversation and conversation.group(2):
            args = {"channel": conversation.group(1), "ts": conversation.group(2)}
            if json.dumps(args, sort_keys=True) not in asked:
                calls.append(use("slack_read_thread", args))
    if calls:
        return [{"type": "text", "text": "Reading the sources."}, *calls]
    points, gaps = [], []
    for result in results:
        if result.lower().startswith(("access denied", "error", "not allowed")):
            gaps.append(result.splitlines()[0])
            continue
        for line in result.splitlines():
            if m := LINE.match(line):
                points.append({"text": f"{m.group(2).strip()}: {m.group(3).strip()[:200]}", "sources": [m.group(1)]})
        if not any(LINE.match(line) for line in result.splitlines()):
            gaps.append(result.splitlines()[0] if result else "No messages found.")
    return [use("submit_findings", {"points": points[-15:], "gaps": gaps})]  # the most recent


SHARE = re.compile(r"\b(?:post|share|send)\b[^#]*#([a-z0-9][a-z0-9_-]*)", re.IGNORECASE)


def plan(user: str) -> dict[str, Any]:
    try:
        goal = json.loads(tag(user, "brief") or "{}").get("goal", "")
    except ValueError:
        goal = ""
    match = SHARE.search(goal)
    if not match:
        return {"actions": [], "notes": "Answer from the gathered findings."}
    return {
        "actions": [
            {
                "tool": "slack",
                "operation": "post_message",
                "args": {"channel": match.group(1), "text": "{answer}"},
                "summary": f"Post the summary in #{match.group(1)}",
            }
        ],
        "notes": "Compose the answer, then share it.",
    }


def report(user: str) -> dict[str, Any]:
    try:
        findings = json.loads(tag(user, "findings") or "{}")
    except ValueError:
        findings = {}
    points = findings.get("points") or []
    gaps = findings.get("gaps") or []
    if not points:
        reason = "; ".join(gaps) if gaps else "I found nothing relevant."
        return {
            "answer": f"I couldn't answer that. {reason}",
            "used_sources": [],
            "confidence": "low",
            "not_verified": [],
        }
    lines = ["Here's what I found:"]
    used = []
    for point in points:
        refs = [str(n) for n in point.get("sources", [])]
        used += refs
        lines.append(f"• {point['text']} " + " ".join(f"[{n}]" for n in refs))
    if gaps:
        lines.append("_Not covered: " + "; ".join(gaps) + "_")
    return {"answer": "\n".join(lines), "used_sources": used, "confidence": "high", "not_verified": []}


ISSUE_LINE = re.compile(r"^\s*\[([A-Z]\d+)\]\s+\(([^)]*)\)\s+([A-Z][A-Z0-9]+-\d+):\s?(.*)$")


def narrative(user: str) -> dict[str, Any]:
    """A scheduled report's narrative: highlights from the first Jira issues (most recently updated
    first) or the latest Slack messages, each citing its line; a summary without numbers."""
    lines = (tag(user, "sources") or "").splitlines()
    issues = [m for line in lines if (m := ISSUE_LINE.match(line))]
    if issues:
        highlights = []
        for m in issues[:4]:
            meta = [part.strip() for part in m[2].split("·")]
            assignee = next((p.removeprefix("assignee ") for p in meta if p.startswith("assignee ")), "unassigned")
            highlights.append({"text": f"{m[3]}: {m[4]} ({meta[0]}, {assignee})", "refs": [m[1]]})
        return {"summary": "Here is how the project moved in this period.", "highlights": highlights}
    messages = [m for line in lines if not line.lstrip().startswith("↳") and (m := LINE.match(line))]
    highlights = [{"text": f"{m[2]}: {m[3][:160]}", "refs": [m[1]]} for m in messages[-4:]]
    summary = "The main points from this period are below." if highlights else "It was a quiet period."
    return {"summary": summary, "highlights": highlights}


def build_router(anthropic: Anthropic) -> APIRouter:
    router = APIRouter()

    @router.post("/anthropic/v1/messages")
    async def messages(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(error("invalid_request_error", "body must be JSON"), status_code=400)
        status, payload, headers = anthropic.handle(body, request.headers.get("x-api-key"))
        return JSONResponse(
            payload, status_code=status, headers={"request-id": f"req_{uuid.uuid4().hex[:16]}", **headers}
        )

    @router.post("/_admin/anthropic/faults")
    async def faults(request: Request) -> dict[str, int]:
        p = await request.json()
        with anthropic.state.lock:
            anthropic.state.faults.append(Fault(int(p["status"]), int(p.get("count", 1)), p.get("retry_after")))
            return {"queued": len(anthropic.state.faults)}

    @router.post("/_admin/anthropic/script")
    async def script(request: Request) -> dict[str, int]:
        """Queue responses: each item is a list of content blocks returned verbatim, in order."""
        p = await request.json()
        with anthropic.state.lock:
            for content in p["responses"]:
                anthropic.state.scripts.append({"content": content})
            return {"queued": len(anthropic.state.scripts)}

    @router.get("/_admin/anthropic/requests")
    def requests() -> list[dict[str, Any]]:
        with anthropic.state.lock:
            return list(anthropic.state.requests)

    @router.post("/_admin/anthropic/reset")
    def reset() -> dict[str, str]:
        anthropic.reset()
        return {"status": "reset"}

    return router

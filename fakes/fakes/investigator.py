"""The Anthropic stand-in's investigations (heuristics, not a model).

Investigators are recognised by the tools on offer and the ``<case>`` the platform sends:

- logs: errors and warnings of the case's service in its window, then the trace of a sample;
- code: the repository's files, then the files whose paths share words with the issue;
- changes: commits around the window and deployments, then the details of the newest commits.

Findings are the tool result lines (``[L1] ...``), with code clues (comments marked BUG, TODO, or
explaining a choice) kept in the text. The plan joins the leading error, the file it points to (or
the file with a clue), the code read there and the commit that changed it.
"""

import json
import re
from datetime import datetime, timedelta
from typing import Any

REF_LINE = re.compile(r"^\[([A-Z]\d+)\]\s+(.*)$")
ERROR = re.compile(
    r"^(Access denied|Not available|Code is not available|No |Google Cloud|GitHub refused|GitHub error|"
    r"The catalog|A window|.*is not connected|.*refused)",
)
FRAME = re.compile(r"@ ([\w./-]+):(\d+) in (\w+)")
CLUE = re.compile(r"(#|//)\s*(BUG|TODO|FIXME|XXX|read once|read on every|preallocated)", re.IGNORECASE)
STOP = {"the", "and", "for", "with", "are", "is", "from", "after", "since", "this", "that", "fails", "fail",
        "returns", "error", "errors", "issue", "bug", "points", "trace", "stack", "more", "our"}  # fmt: skip


def words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if len(w) >= 3 and w not in STOP:
            out.add(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return out


def use(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "name": name, "input": payload}


def _tag(text: str, name: str) -> str | None:
    m = re.search(rf"<{name}>(.*?)</{name}>", text, re.DOTALL)
    return m.group(1).strip() if m else None


def _history(messages: list[dict[str, Any]]) -> tuple[set[str], list[tuple[str, str]]]:
    """(the inputs already sent, (tool name, result text) pairs in order)."""
    asked: set[str] = set()
    names: dict[str, str] = {}
    results: list[tuple[str, str]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                asked.add(json.dumps({"n": block.get("name"), "i": block.get("input")}, sort_keys=True))
                names[str(block.get("id"))] = str(block.get("name"))
            elif block.get("type") == "tool_result":
                body = block.get("content")
                text = body if isinstance(body, str) else "\n".join(b.get("text", "") for b in body or [])
                results.append((names.get(str(block.get("tool_use_id")), ""), text))
    return asked, results


def _kind(tools: set[str]) -> str:
    if any(t.endswith("_search_events") for t in tools):
        return "logs"
    if "github_search_code" in tools or "github_read_code" in tools:
        return "code"
    return "changes"


def _shift(iso: str | None, days: float) -> str | None:
    if not iso:
        return None
    try:
        return (datetime.fromisoformat(iso.replace("Z", "+00:00")) + timedelta(days=days)).isoformat()
    except ValueError:
        return None


def first_issue(case: dict[str, Any]) -> dict[str, Any]:
    """The case's first issue (the platform sends ``issues``; before several issues, ``issue``)."""
    issues = case.get("issues") or ([case["issue"]] if case.get("issue") else [])
    return dict(issues[0]) if issues else {}


def _wanted(kind: str, case: dict[str, Any], results: list[tuple[str, str]], tools: set[str]) -> list[dict[str, Any]]:
    service = case.get("service") or {}
    name, repo = service.get("name"), service.get("repository")
    window = case.get("window") or {}
    issue = first_issue(case)
    calls: list[dict[str, Any]] = []
    if kind == "logs" and name:
        search = next((t for t in tools if t.endswith("_search_events")), None)
        if search:
            calls.append(use(search, {"service": name, "start": window.get("start"), "end": window.get("end"),
                                      "severity": "WARNING"}))  # fmt: skip
        traces = [m.group(1) for _, text in results for m in [re.search(r"\btrace ([0-9a-f]{16,})", text)] if m]
        trace_tool = next((t for t in tools if t.endswith("_get_trace")), None)
        if traces and trace_tool:
            calls.append(use(trace_tool, {"service": name, "trace_id": traces[0]}))
    if kind == "code" and repo:
        calls.append(use("github_list_files", {"repo": repo, "recursive": True}))
        listing = next((text for tool, text in results if tool == "github_list_files"), "")
        about = words(" ".join(str(x) for x in (case.get("goal"), issue.get("summary"), issue.get("description"))))
        files = [line.strip() for line in listing.splitlines()[1:] if line.strip() and not line.endswith("/")]
        ranked = sorted(files, key=lambda p: -len(words(p.replace("/", " ").replace(".", " ")) & about))
        for path in [p for p in ranked if words(p.replace("/", " ").replace(".", " ")) & about][:2]:
            calls.append(use("github_read_code", {"repo": repo, "path": path}))
    if kind == "changes" and repo:
        calls.append(use("github_list_commits", {"repo": repo, "since": _shift(window.get("start"), -3),
                                                 "until": window.get("end")}))  # fmt: skip
        calls.append(use("github_list_deployments", {"repo": repo}))
        listed = next((text for tool, text in results if tool == "github_list_commits"), "")
        for sha in re.findall(r"^\[[A-Z]\d+\] ([0-9a-f]{7}) ", listed, re.MULTILINE)[:3]:
            calls.append(use("github_get_commit", {"repo": repo, "sha": sha}))
    return calls


def _points(results: list[tuple[str, str]]) -> tuple[list[dict[str, Any]], list[str]]:
    points: list[dict[str, Any]] = []
    gaps: list[str] = []
    for tool, text in results:
        lines = text.splitlines()
        if not any(REF_LINE.match(line) for line in lines):
            if ERROR.match(text):  # a refusal or a failure; listings without refs are fine
                gaps.append(f"{tool}: {lines[0]}"[:200])
            continue
        current: dict[str, Any] | None = None
        for line in lines:
            m = REF_LINE.match(line)
            if m:
                current = {"text": m.group(2)[:300], "sources": [m.group(1)]}
                points.append(current)
            elif current is not None and CLUE.search(line):
                current["text"] = (current["text"] + " | " + line.strip())[:600]
    return points[:25], gaps


def investigate(messages: list[dict[str, Any]], first_user: str, tools: set[str], forced: bool) -> list[dict[str, Any]]:
    try:
        case = json.loads(_tag(first_user, "case") or "{}")
    except ValueError:
        case = {}
    kind = _kind(tools)
    asked, results = _history(messages)
    if not forced:
        calls = [c for c in _wanted(kind, case, results, tools)
                 if json.dumps({"n": c["name"], "i": c["input"]}, sort_keys=True) not in asked]  # fmt: skip
        if calls:
            return [{"type": "text", "text": f"Investigating {kind}."}, *calls]
    points, gaps = _points(results)
    if not (case.get("service") or {}).get("repository" if kind != "logs" else "name"):
        gaps.append(f"{kind}: the case names no {'repository' if kind != 'logs' else 'service'}")
    return [use("submit_findings", {"points": points, "gaps": gaps})]


def plan(user: str) -> dict[str, Any]:
    """A plan from the findings: the leading error, the file it points to, the clue in its code and
    the commit that changed it."""
    try:
        findings = json.loads(_tag(user, "findings") or "{}")
        case = json.loads(_tag(user, "case") or "{}")
    except ValueError:
        findings, case = {}, {}
    prior = _tag(user, "prior_plan")
    points = findings.get("points") or []
    by = {k: [p for p in points if p.get("from") == k] for k in ("logs", "code", "changes", "case")}
    errors = [p for p in by["logs"] if re.search(r"\b(ERROR|CRITICAL|WARNING) x\d+", p["text"])]
    top = errors[0] if errors else None
    frame = FRAME.search(top["text"]) if top else None
    clue_code = [p for p in by["code"] if " | " in p["text"]]
    path = frame.group(1) if frame else None
    if path is None and clue_code:
        path = (
            re.split(r"\s", clue_code[0]["text"].split("@", 1)[-1], maxsplit=2)[1]
            if "@" in clue_code[0]["text"]
            else None
        )
    code = next((p for p in clue_code if path and path in p["text"]), clue_code[0] if clue_code else None)
    commits = [p for p in by["changes"] if path and path in p["text"]]
    commit = next((p for p in commits if " | " in p["text"]), commits[0] if commits else None)
    repo = (case.get("service") or {}).get("repository")
    evidence = [{"text": p["text"][:240], "sources": p["sources"]} for p in (top, code, commit) if p]
    used = sorted({n for p in (top, code, commit) if p for n in p["sources"]})
    clue = next((part.strip() for p in (code, commit) if p for part in p["text"].split(" | ")[1:]), "")
    cause = ""
    if commit:
        message = re.sub(r"^[0-9a-f]{7} \S+ \S+ \S+: ", "", commit["text"].split(" | ")[0]).split(" (")[0]
        cause = f'The change "{message}"'
    if path:
        cause = (cause + " in " if cause else "") + path
    if clue:
        cause += f": {clue}"
    if top:
        cause += f", which leads to {top['text'].split('): ', 1)[-1].split('. Samples')[0][:160]}"
    kinds = sum(1 for p in (top, code, commit) if p)
    confidence = "high" if kinds == 3 else "medium" if kinds == 2 else "low"
    summary = f"The most likely cause: {cause}." if cause else "The evidence is too thin to name a cause yet."
    goal = case.get("goal") or "Fix the reported problem"
    revision = {"changed": False, "what_changed": ""}
    if prior:
        summary = f"On your follow-up ({goal[:120]}): " + summary
        revision = {"changed": True, "what_changed": f"Rechecked with the follow-up: {goal[:160]}"}
    files = [{"repo": repo, "path": path, "why": "where the problem is"}] if repo and path else []
    return {
        "summary": summary,
        "goal": first_issue(case).get("summary") or goal,
        "evidence": evidence,
        "hypotheses": [{"text": cause or "Unknown: no error, code or change found", "confidence": confidence,
                        "evidence": used, "confirm": "A failing test that reproduces it"}],
        "steps": [
            {"kind": "reproduce", "text": f"Write a failing test for {path or 'the problem'} that reproduces the issue"},
            {"kind": "implement", "text": f"Fix {path or 'the cause'} and keep the change small"},
            {"kind": "verify", "text": "Run the tests, then watch the error rate in the logs after the deploy"},
        ],
        "files": files,
        "risks": ["Other callers may depend on the current behavior"],
        "verification": "The new test passes and the errors stop in the logs.",
        "rollback": f"Revert the fix (or the change that caused it){' in ' + repo if repo else ''}.",
        "open_questions": [] if confidence == "high" else ["Can someone in the thread confirm when it started?"],
        "confidence": confidence,
        "revision": revision,
    }  # fmt: skip

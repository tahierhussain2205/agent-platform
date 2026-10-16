"""Report figures are data (spec 4.6.6): computed here from tool data, rendered by code above the
narrative, and the only numbers the narrative may use besides those in the sources it cites."""

import re
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

PRIORITY_ORDER = ["Highest", "High", "Medium", "Low", "Lowest"]
REF = re.compile(r"\[[A-Z]\d+\]")
ISSUE_KEY = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
DATE_OR_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b|\b\d{1,2}:\d{2}\b")
NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*")


def parse_jira_time(value: str | None) -> datetime | None:
    """Jira's ``2026-10-16T09:00:00.000+0000`` (and plain ISO 8601)."""
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt).astimezone(UTC)
        except ValueError:
            continue
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _within(value: str | None, start: datetime, end: datetime) -> bool:
    when = parse_jira_time(value)
    return when is not None and start <= when < end


def channel_figures(stats: list[dict[str, Any]]) -> dict[str, Any]:
    """Per channel and total activity from ``slack.channel_stats`` data. People are counted once
    across channels."""
    people: set[str] = set()
    channels = []
    for s in stats:
        people |= set(s.get("people") or [])
        channels.append(
            {
                "channel": s["channel"],
                "messages": int(s["messages"]),
                "threads": int(s["threads"]),
                "replies": int(s["replies"]),
                "people": len(s.get("people") or []),
            }
        )
    return {
        "channels": channels,
        "total": {
            "messages": sum(c["messages"] for c in channels),
            "threads": sum(c["threads"] for c in channels),
            "replies": sum(c["replies"] for c in channels),
            "people": len(people),
        },
        "truncated": any(s.get("truncated") for s in stats),
    }


def _ordered(counts: Counter[str], order: list[str] | None = None) -> list[list[Any]]:
    rank = {name: i for i, name in enumerate(order or [])}
    ordered = sorted(counts.items(), key=lambda kv: (rank.get(kv[0], len(rank)), -kv[1], kv[0]))
    return [[k, n] for k, n in ordered]


def jira_figures(
    project: str, issues: list[dict[str, Any]], start: datetime, end: datetime
) -> dict[str, Any]:
    """Created and resolved in [start, end), and what is open now, from ``jira.report_issues`` data."""
    created = [i for i in issues if _within(i.get("created"), start, end)]
    resolved = [i for i in issues if _within(i.get("resolved"), start, end)]
    open_now = [i for i in issues if i.get("category") != "done"]
    return {
        "project": project,
        "created": len(created),
        "resolved": len(resolved),
        "open": len(open_now),
        "open_by_status": _ordered(Counter(str(i.get("status") or "No status") for i in open_now)),
        "open_by_priority": _ordered(
            Counter(str(i.get("priority") or "No priority") for i in open_now), PRIORITY_ORDER
        ),
        "unassigned_open": sum(1 for i in open_now if not i.get("assignee")),
        "created_keys": sorted(i["key"] for i in created),
        "resolved_keys": sorted(i["key"] for i in resolved),
    }


def _pairs(pairs: list[list[Any]]) -> str:
    return ", ".join(f"{name} {n}" for name, n in pairs) or "none"


def figure_lines(kind: str, figures: dict[str, Any]) -> list[str]:
    if kind == "jira_progress":
        lines = [
            f"Created: *{figures['created']}* · Resolved: *{figures['resolved']}* · "
            f"Open now: *{figures['open']}*",
            f"Open by status: {_pairs(figures['open_by_status'])}",
            f"Open by priority: {_pairs(figures['open_by_priority'])}",
        ]
        if figures["unassigned_open"]:
            lines.append(f"Open and unassigned: {figures['unassigned_open']}")
        return lines
    total = figures["total"]
    lines = [
        f"Messages: *{total['messages']}* · People: *{total['people']}* · "
        f"Threads: *{total['threads']}* ({total['replies']} replies)"
    ]
    if len(figures["channels"]) > 1:
        lines += [
            f"#{c['channel']}: {c['messages']} messages, {c['people']} people, {c['threads']} threads"
            for c in figures["channels"]
        ]
    if figures.get("truncated"):
        lines.append("_Very busy period: counts cover the first 10,000 messages._")
    return lines


def _flatten(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for v in value.values():
            yield from _flatten(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _flatten(v)
    else:
        yield value


def numbers(text: str) -> set[str]:
    """Numbers a text states, ignoring refs ([S3]), issue keys (PAY-101), dates and clock times."""
    plain = DATE_OR_TIME.sub(" ", ISSUE_KEY.sub(" ", REF.sub(" ", text)))
    return {m.group(0).replace(",", "") for m in NUMBER.finditer(plain)}


def figure_numbers(figures: dict[str, Any]) -> set[str]:
    return {str(v) for v in _flatten(figures) if isinstance(v, int) and not isinstance(v, bool)}


def verify_narrative(
    summary: str,
    highlights: list[dict[str, Any]],
    figures: dict[str, Any],
    lines_by_ref: dict[str, str],
) -> tuple[str, list[dict[str, Any]], int]:
    """Keep what states only known numbers: the figures, or numbers in the source lines a highlight
    cites. Highlights must cite refs that exist. Returns (summary, highlights, how many dropped)."""
    known = figure_numbers(figures)
    dropped = 0
    kept: list[dict[str, Any]] = []
    for h in highlights:
        refs = [r for r in h.get("refs") or [] if r in lines_by_ref]
        text = str(h.get("text") or "").strip()
        cited = set().union(*(numbers(lines_by_ref[r]) for r in refs)) if refs else set()
        if not text or not refs or not numbers(text) <= known | cited:
            dropped += 1
            continue
        kept.append({"text": text, "refs": refs})
    summary = summary.strip()
    if summary and not numbers(summary) <= known:
        summary, dropped = "", dropped + 1
    return summary, kept, dropped

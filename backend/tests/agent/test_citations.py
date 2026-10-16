"""Citations and gaps from model text, checked in code (findings from the first real model runs)."""

from app.agent.runner import AgentRunner, expand_citations
from app.tools.base import RefBook


def test_citation_ranges_and_lists_keep_every_source() -> None:
    en_dash = "\u2013"  # as the model writes it
    assert (
        expand_citations(f"rolled back [4]{en_dash}[9] again") == "rolled back [4] [5] [6] [7] [8] [9] again"
    )
    assert expand_citations("see [2-4]") == "see [2] [3] [4]"
    assert expand_citations("see [1, 3, 5]") == "see [1] [3] [5]"
    assert expand_citations("plain [2] [3]") == "plain [2] [3]"
    assert expand_citations("not a range [9]-[2]") == "not a range [9]-[2]"
    assert expand_citations("too wide [1]-[90]") == "too wide [1]-[90]"  # each end still counts on its own


def test_what_could_not_be_read_is_reported_in_the_tools_words() -> None:
    raw = {
        "points": [],
        "gaps": ["Could not read #leads-private: access was denied", "Nothing about refunds"],
    }
    denied = ["#leads-private: you are not a member of #leads-private"]
    findings = AgentRunner._findings(raw, RefBook(), denied, [])
    assert findings.gaps == [
        "#leads-private: you are not a member of #leads-private",
        "Nothing about refunds",
    ]


async def test_channels_the_brief_names_but_were_not_read_are_reported() -> None:
    from types import SimpleNamespace
    from typing import Any

    from app.agent.types import Findings
    from app.policy import Decision

    class Slack:
        async def resolve_target(self, ctx: Any, operation: str, args: dict[str, Any]) -> str:
            return str(args["channel"])

    class Policy:
        async def check_tool(self, adapter: str, operation: str, target: str) -> Decision:
            if target == "leads-private":
                return Decision(
                    "denied",
                    "not_a_member",
                    "You are not a member of #leads-private, so I can't use it for you.",
                )
            return Decision("allowed", "ok", "Read access.")

    executor: Any = SimpleNamespace(
        registry=SimpleNamespace(adapters={"slack": Slack()}), policy=Policy(), ctx=None
    )
    brief = {"entities": {"channels": ["leads-private", "bugs", "random"]}}
    findings = Findings([], ["Could not access #leads-private"], ["#bugs"])
    await AgentRunner._unread_channels(executor, brief, findings)
    assert findings.gaps == [
        "#random: not read",
        "#leads-private: You are not a member of #leads-private, so I can't use it for you.",
    ]


def test_a_brief_window_of_a_moment_is_not_a_window() -> None:
    from datetime import UTC, datetime

    from app.agent.investigation import window_of

    now = datetime(2026, 10, 16, 15, 28, 54, tzinfo=UTC)
    moment = {"start": "2026-10-16T15:28:50+00:00", "end": None}  # "why is search slow" read as "now"
    issue = {"created": "2026-10-15T11:00:00.000+0000", "status": "To Do"}
    assert window_of(moment, issue, now) == ["2026-10-15T09:00:00+00:00", now.isoformat()]  # the issue's


def test_eval_root_causes_ignore_spacing_and_accept_wordings() -> None:
    from app.eval import score

    case = {"expect": {"root_cause": ["MAX_LINES = 19", ["read once at import", "import time"]]}}
    good = {"hypothesis": "The preallocation (MAX_LINES=19) and the key cached at module import time"}
    assert score(case, good)["root_cause"] == 1.0
    assert score(case, {"hypothesis": "MAX_LINES=19 only"})["root_cause"] == 0.0

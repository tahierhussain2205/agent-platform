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

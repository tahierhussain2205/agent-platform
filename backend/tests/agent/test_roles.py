"""Role profiles and limits as code reads them (D-202, D-203): profiles become our own sentences,
stored values that no longer validate are dropped, and the most restrictive limit wins."""

import pytest
from pydantic import ValidationError

from app.agent import prompts
from app.agent.roles import Profile, combine, guidance, names, profile_of


def test_a_profile_reads_as_fixed_sentences() -> None:
    profile = Profile(detail="brief", style="business", format="summary_first", focus=["risks", "next_steps"])
    text = guidance(profile)
    assert text.startswith("Keep answers short")
    assert "Speak to impact" in text
    assert "Start with a one line summary" in text
    assert text.endswith("Call out risks and next steps.")
    assert guidance(Profile()) == ""


def test_profiles_refuse_unknown_values_and_stored_ones_are_read_leniently() -> None:
    with pytest.raises(ValidationError):
        Profile.model_validate({"detail": "very long"})
    with pytest.raises(ValidationError):
        Profile.model_validate({"tone": "pirate"})  # no free text fields in a profile
    assert Profile(focus=["risks", "risks"]).focus == ["risks"]
    stored = profile_of({"detail": "brief", "style": "shouty", "focus": ["owners", "gossip"], "x": 1})
    assert stored == Profile(detail="brief", focus=["owners"])
    assert profile_of("not an object") == Profile()


def test_the_role_section_carries_the_profile_and_the_description() -> None:
    section = prompts.role_section(
        [
            prompts.RoleInfo("Support", "Customers first.", True, Profile(detail="brief", style="plain")),
            prompts.RoleInfo("QA", "", False, Profile(focus=["code"])),
        ]
    )
    lines = section.splitlines()
    assert lines[2] == (
        "Primary role: Support. Keep answers short: the essentials in a few bullets or two or three sentences. "
        "Use plain language without jargon, and explain any technical term you need. "
        "In their words: Customers first."
    )
    assert lines[3] == "Also: QA. Call out the code involved."
    assert "never change what you may read or do" in section


def test_the_most_restrictive_limit_wins() -> None:
    limits = combine(
        [
            ("Support", ["slack_qa", "jira_tickets"], ["github.push_changes"], [], "ask"),
            ("QA", ["jira_tickets", "investigate_issue"], [], ["jira.create_issue"], None),
            ("Reader", None, ["github.push_changes"], [], "auto"),
        ]
    )
    assert limits.playbooks == frozenset({"jira_tickets"})
    assert limits.playbooks_by == ("Support", "QA")
    assert limits.allows("jira_tickets")
    assert not limits.allows("implement_change")
    assert limits.blocked == {"github.push_changes": "Support"}
    assert limits.ask == {"jira.create_issue": "QA"}
    assert limits.capped_by == "Support"
    none = combine([])
    assert none.allows("implement_change")
    assert none.capped_by is None
    assert names(["Support", "QA", "Support"]) == "Support and QA"

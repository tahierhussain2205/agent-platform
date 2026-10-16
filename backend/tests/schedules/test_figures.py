"""Figures by code, and the narrative check that keeps unknown numbers out of reports."""

from datetime import UTC, datetime

from app.schedules.figures import (
    channel_figures,
    figure_lines,
    jira_figures,
    numbers,
    parse_jira_time,
    verify_narrative,
)

START = datetime(2026, 10, 15, 7, 0, tzinfo=UTC)
END = datetime(2026, 10, 16, 7, 0, tzinfo=UTC)


def issue(key: str, status: str, category: str, priority: str, created: str, resolved: str | None = None,
          assignee: str | None = "Bob") -> dict[str, object]:  # fmt: skip
    return {"key": key, "status": status, "category": category, "priority": priority, "created": created,
            "resolved": resolved, "assignee": assignee}  # fmt: skip


ISSUES = [
    issue("PAY-1", "To Do", "new", "Medium", "2026-10-15T08:00:00.000+0000", assignee=None),
    issue("PAY-2", "In Progress", "indeterminate", "High", "2026-10-10T08:00:00.000+0000"),
    issue("PAY-3", "Done", "done", "Low", "2026-10-15T09:00:00.000+0000", "2026-10-16T06:59:59.000+0000"),
    issue("PAY-4", "Done", "done", "High", "2026-10-01T09:00:00.000+0000", "2026-10-16T07:00:00.000+0000"),
    issue("PAY-5", "To Do", "new", "Highest", "2026-10-15T07:00:00.000+0000"),  # exactly at the start
]


def test_jira_figures_count_the_window_and_what_is_open() -> None:
    f = jira_figures("PAY", ISSUES, START, END)
    assert (f["created"], f["resolved"], f["open"]) == (3, 1, 3)
    assert f["created_keys"] == ["PAY-1", "PAY-3", "PAY-5"]  # start inclusive
    assert f["resolved_keys"] == ["PAY-3"]  # end exclusive: PAY-4 belongs to the next window
    assert f["open_by_status"] == [["To Do", 2], ["In Progress", 1]]
    assert f["open_by_priority"] == [["Highest", 1], ["High", 1], ["Medium", 1]]  # Jira's order
    assert f["unassigned_open"] == 1
    assert figure_lines("jira_progress", f) == [
        "Created: *3* · Resolved: *1* · Open now: *3*",
        "Open by status: To Do 2, In Progress 1",
        "Open by priority: Highest 1, High 1, Medium 1",
        "Open and unassigned: 1",
    ]


def test_empty_project() -> None:
    f = jira_figures("OPS", [], START, END)
    assert figure_lines("jira_progress", f)[:2] == [
        "Created: *0* · Resolved: *0* · Open now: *0*",
        "Open by status: none",
    ]


def test_channel_figures_count_people_once() -> None:
    f = channel_figures(
        [
            {"channel": "bugs", "messages": 5, "threads": 2, "replies": 4, "people": ["U1", "U2"]},
            {"channel": "general", "messages": 3, "threads": 0, "replies": 0, "people": ["U2", "U3"]},
        ]
    )
    assert f["total"] == {"messages": 8, "threads": 2, "replies": 4, "people": 3}
    assert figure_lines("channel_summary", f) == [
        "Messages: *8* · People: *3* · Threads: *2* (4 replies)",
        "#bugs: 5 messages, 2 people, 2 threads",
        "#general: 3 messages, 2 people, 0 threads",
    ]


def test_parse_jira_time() -> None:
    assert parse_jira_time("2026-10-15T08:00:00.000+0000") == datetime(2026, 10, 15, 8, tzinfo=UTC)
    assert parse_jira_time("2026-10-15T10:00:00.000+0200") == datetime(2026, 10, 15, 8, tzinfo=UTC)
    assert parse_jira_time(None) is None


def test_numbers_ignore_refs_keys_dates_and_times() -> None:
    assert numbers("PAY-101 was fixed [S3] on 2026-10-16 at 09:30 after 3 retries") == {"3"}
    assert numbers("Carts with 20 or more items, v41 rolled back, 1,200 users, 4.5 s") == {
        "20",
        "1200",
        "4.5",
    }


def test_narrative_keeps_only_known_numbers() -> None:
    figures = jira_figures("PAY", ISSUES, START, END)
    lines = {
        "S1": "[S1] (To Do · Medium) PAY-1: Invoice totals are 1 cent low",
        "S2": "[S2] (Done · Low) PAY-3: Add audit entries",
    }
    summary, kept, dropped = verify_narrative(
        "3 issues were created and 1 was resolved.",
        [
            {"text": "PAY-1 is unassigned: totals are 1 cent low.", "refs": ["S1"]},  # 1 from its source
            {"text": "PAY-3 was resolved.", "refs": ["S2"]},
            {"text": "About 12 issues are stuck.", "refs": ["S1"]},  # invented
            {"text": "PAY-9 is late.", "refs": ["S9"]},  # cites nothing that exists
            {"text": "Things moved.", "refs": []},  # cites nothing
        ],
        figures,
        lines,
    )
    assert summary == "3 issues were created and 1 was resolved."
    assert [h["text"] for h in kept] == ["PAY-1 is unassigned: totals are 1 cent low.", "PAY-3 was resolved."]
    assert dropped == 3


def test_a_summary_with_an_invented_number_is_dropped() -> None:
    summary, _, dropped = verify_narrative(
        "Velocity rose 40%.", [], jira_figures("PAY", ISSUES, START, END), {}
    )
    assert (summary, dropped) == ("", 1)

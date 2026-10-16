"""Values passed between the task workflow and its activities.

They go into Temporal history, so they hold ids, derived content (brief items, findings, the
answer) and source references (channel, ts, permalink) only, never raw Slack message text
(invariant 7). Plain dataclasses of JSON types, for Temporal's default converter.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskRef:
    """Which task, for whom, and where its conversation is."""

    org_id: str
    task_id: str
    session_id: str
    workspace_id: str
    team_id: str
    channel: str  # external id of the conversation (C..., G... or D...)
    channel_type: str  # channel | group | im
    channel_id: str | None  # the tracked channel row; None for DMs
    thread_ts: str | None  # where replies go (None: top level of a DM)
    message_ts: str  # the message that asked
    membership_id: str
    slack_user_id: str
    trigger: str = "mention"
    lane: str = "interactive"


@dataclass
class StartResult:
    status: str  # started | wait_session | wait_org | refused | gone
    message: str = ""
    mode: str = "ask"
    model_tier: str = "standard"


@dataclass
class Understanding:
    playbook_key: str | None
    playbook_version: int | None
    confidence: float
    question: str | None
    mode: str
    mode_note: str | None = None
    brief: dict[str, Any] | None = None
    stopped: str | None = None


@dataclass
class Findings:
    points: list[dict[str, Any]]  # {"text", "sources": [source dicts]}
    gaps: list[str]
    channels_read: list[str] = field(default_factory=list)
    stopped: str | None = None


@dataclass
class PlannedAction:
    tool: str
    operation: str
    args: dict[str, Any]
    summary: str
    evidence: list[str] = field(default_factory=list)  # why, for approvers (an assignee's reasons...)
    skip: str | None = None  # dropped by the checks in code, with the reason for the thread
    ask: bool = False  # Ask even in Auto (a large plan, private sources)


@dataclass
class Composed:
    answer: str
    sources: list[dict[str, Any]]  # numbered in order: [1] is sources[0]
    confidence: str
    verified: int
    total: int
    not_verified: list[str] = field(default_factory=list)
    stopped: str | None = None


@dataclass
class ActionOutcome:
    status: str  # done | awaiting_approval | denied | failed | skipped
    message: str
    approval_id: str | None = None
    expires_at: str | None = None
    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class ApprovalState:
    status: str  # pending | approved | rejected | expired
    decided_by: str | None = None


@dataclass
class BudgetCheck:
    ok: bool
    message: str = ""


@dataclass
class Delivered:
    where: str  # thread | private | dm
    ts: str | None = None


@dataclass
class ReportInput:
    """What a Temporal Schedule's action passes to each run."""

    org_id: str
    schedule_id: str


@dataclass
class ReportStart:
    status: str  # run | skip (deleted, paused, or paused now: see message)
    message: str = ""
    ref: TaskRef | None = None
    playbook_key: str = ""
    title: str = ""
    window_start: str = ""  # ISO 8601, UTC
    window_end: str = ""
    scheduled_at: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    delivery: str = "dm"  # channel | dm
    delivery_name: str = ""
    model_tier: str = "standard"
    paused: bool = False  # this run paused the schedule (admins must hear about it)


@dataclass
class ReportComposed:
    figures: dict[str, Any]
    figure_lines: list[str]
    summary: str
    highlights: list[dict[str, Any]]  # {"text", "refs": [numbers into sources, 1 based]}
    sources: list[dict[str, Any]]
    dropped: int = 0
    denied: str | None = None  # the owner can no longer read a source: the schedule pauses
    stopped: str | None = None  # a budget is spent: the schedule pauses

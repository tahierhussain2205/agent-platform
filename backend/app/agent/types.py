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

"""Workflow inputs and outputs for Slack work. References only: never message text (invariant 7:
raw Slack messages are not stored, and Temporal history is storage)."""

from dataclasses import dataclass, field


@dataclass
class SlackRequest:
    org_id: str
    workspace_id: str
    team_id: str
    channel: str
    channel_type: str  # channel | group | im | mpim
    user: str
    ts: str
    thread_ts: str | None
    event_id: str
    event_type: str
    intent: str | None = None  # a quick action's key (a button on an answer), never text


@dataclass
class Requester:
    membership_id: str | None
    email: str | None = None
    status: str | None = None


@dataclass
class ChannelCheck:
    allowed: bool
    reason: str
    channel_id: str | None = None


@dataclass
class SyncRequest:
    org_id: str
    workspace_id: str
    team_id: str
    channel_ids: list[str] = field(default_factory=list)  # empty: every allowlisted channel


@dataclass
class SyncResult:
    channels: int
    member_lists: int
    archived: int


@dataclass
class HomeRequest:
    org_id: str
    workspace_id: str
    team_id: str
    user: str

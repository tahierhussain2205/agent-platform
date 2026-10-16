"""The tool adapter interface.

Adding a tool means writing an adapter and its operation metadata; the policy module and the
audit log apply automatically. Metadata lives in code (spec 13.3): whether an operation reads or
writes, whether it can be undone, whether it reaches people outside the conversation, and its
default credential mode. The action tier (spec 4.5) derives from that.
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import UUID

ActionTier = Literal["read", "reversible_write", "irreversible"]
CredentialMode = Literal["org", "org_with_check", "user_required"]
ApproverRule = Literal["requester", "admin", "requester_or_admin"]
# restricted: visible to some people only (a Jira project, a private repository, someone's Drive);
# whether a whole channel may see it is asked of the adapter (``audience_ok``).
Visibility = Literal["public", "private", "dm", "restricted"]

RESULT_CAP = 12_000  # characters of tool output the model sees per call


@dataclass(frozen=True)
class Operation:
    name: str
    description: str
    input_schema: dict[str, Any]
    kind: Literal["read", "write"] = "read"
    reversible: bool = True
    outward_facing: bool = False
    credential_mode: CredentialMode = "org"
    approver_rule: ApproverRule = "requester_or_admin"
    # Called by code only (report figures), never offered to a model; policy and audit still apply.
    internal: bool = False

    @property
    def tier(self) -> ActionTier:
        if self.kind == "read":
            return "read"
        return "irreversible" if (not self.reversible or self.outward_facing) else "reversible_write"


@dataclass(frozen=True)
class Source:
    """Where content came from. Drives citations (permalinks) and the audience check."""

    ref: str  # stable: slack:<channel>:<ts>, jira:<key>, github:<repo>:<path>, mcp:<server>:<tool>
    channel_external_id: str  # Slack only ("" otherwise)
    channel_name: str  # a channel, project, repository or server, for labels and refusals
    visibility: Visibility
    ts: str  # Slack only
    label: str = ""
    permalink: str | None = None
    kind: str = "slack"  # slack | jira | github | mcp
    audience: str = ""  # what decides who may see it (a project key, a repository)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Source":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


@dataclass(frozen=True)
class Target:
    """The resource an operation touches, resolved before the policy check."""

    kind: Literal["slack_channel"]
    id: UUID | None
    external_id: str
    name: str
    is_private: bool
    is_dm: bool = False

    @property
    def visibility(self) -> Visibility:
        return "dm" if self.is_dm else "private" if self.is_private else "public"


@dataclass
class RefBook:
    """Short citation refs (S1, S2, ...) for one model conversation."""

    prefix: str = "S"
    by_ref: dict[str, Source] = field(default_factory=dict)
    by_key: dict[str, str] = field(default_factory=dict)

    def add(self, source: Source) -> str:
        if source.ref in self.by_key:
            return self.by_key[source.ref]
        short = f"{self.prefix}{len(self.by_ref) + 1}"
        self.by_ref[short] = source
        self.by_key[source.ref] = short
        return short

    def get(self, short: str) -> Source | None:
        return self.by_ref.get(str(short).strip("[] "))


@dataclass
class ToolResult:
    text: str
    is_error: bool = False
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolContext:
    """What an adapter needs for one task: who asks, where, and the clients to use."""

    org_id: UUID
    membership_id: UUID
    workspace_id: UUID
    team_id: str
    slack_user_id: str
    token: str
    pool: Any
    slack: Any
    kv: Any
    refs: RefBook = field(default_factory=RefBook)
    task_id: UUID | None = None
    requester_email: str | None = None
    http: Any = None  # httpx.AsyncClient for non Slack adapters
    settings: Any = None  # provider and MCP base URLs
    vault: Any = None
    cache: dict[str, Any] = field(default_factory=dict)  # per task lookups (identities, permissions)
    store: Any = None  # ObjectStore for large artifacts (a change's diff)


class ToolAdapter(Protocol):
    key: str
    operations: dict[str, Operation]

    async def resolve_target(
        self, ctx: ToolContext, operation: str, args: dict[str, Any]
    ) -> Target | None: ...

    async def execute(
        self, ctx: ToolContext, operation: str, args: dict[str, Any], credential: Any = None
    ) -> ToolResult:
        """``credential`` is the ``Credential`` the policy's credential mode chose (None for Slack,
        which uses the bot token). With ``org_with_check``, the adapter must verify the requester
        can access every resource it returns."""
        ...


def tool_name(adapter_key: str, operation: str) -> str:
    """The model facing name (the Messages API allows 64 characters)."""
    return f"{adapter_key}_{operation}"[:64]


def definitions(adapter: ToolAdapter, operations: list[str]) -> list[dict[str, Any]]:
    """Tool definitions for the model, for the operations a step loads."""
    return [
        {
            "name": tool_name(adapter.key, name),
            "description": adapter.operations[name].description,
            "input_schema": adapter.operations[name].input_schema,
        }
        for name in operations
        if not adapter.operations[name].internal
    ]


def cap(text: str, limit: int = RESULT_CAP) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0] + "\n(output truncated; narrow the request to see more)"

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
Visibility = Literal["public", "private", "dm"]

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

    @property
    def tier(self) -> ActionTier:
        if self.kind == "read":
            return "read"
        return "irreversible" if (not self.reversible or self.outward_facing) else "reversible_write"


@dataclass(frozen=True)
class Source:
    """Where content came from. Drives citations (permalinks) and the audience check."""

    ref: str  # stable: slack:<channel>:<ts>
    channel_external_id: str
    channel_name: str
    visibility: Visibility
    ts: str
    label: str = ""
    permalink: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Source":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


@dataclass(frozen=True)
class Target:
    """The resource an operation touches, resolved before the policy check."""

    kind: Literal["slack_channel"]
    id: UUID
    external_id: str
    name: str
    is_private: bool


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


class ToolAdapter(Protocol):
    key: str
    operations: dict[str, Operation]

    async def resolve_target(
        self, ctx: ToolContext, operation: str, args: dict[str, Any]
    ) -> Target | None: ...

    async def execute(self, ctx: ToolContext, operation: str, args: dict[str, Any]) -> ToolResult: ...


def tool_name(adapter_key: str, operation: str) -> str:
    return f"{adapter_key}_{operation}"


def definitions(adapter: ToolAdapter, operations: list[str]) -> list[dict[str, Any]]:
    """Tool definitions for the model, for the operations a step loads."""
    return [
        {
            "name": tool_name(adapter.key, name),
            "description": adapter.operations[name].description,
            "input_schema": adapter.operations[name].input_schema,
        }
        for name in operations
    ]


def cap(text: str, limit: int = RESULT_CAP) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0] + "\n(output truncated; narrow the request to see more)"

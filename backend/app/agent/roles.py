"""Role profiles and limits (D-202, D-203).

A profile shapes how the agent answers the role's holders: fixed choices that code turns into
fixed sentences, so the prompt text is ours (only the description is free text, cleaned and capped
by ``prompts.role_section``). Limits narrow what the agent does for the role's holders and are
enforced in code and the database, never in a prompt: the playbooks they may use, operations
blocked for them, writes that always ask, a cap on their mode, and the operations whose approval
requests the role's holders decide. With several roles the most restrictive wins."""

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Detail = Literal["brief", "balanced", "thorough"]
Style = Literal["plain", "technical", "business"]
Format = Literal["bullets", "paragraphs", "summary_first"]
Focus = Literal["risks", "next_steps", "owners", "dates", "code", "customer_impact", "metrics"]

SENTENCES: dict[str, dict[str, str]] = {
    "detail": {
        "brief": "Keep answers short: the essentials in a few bullets or two or three sentences.",
        "balanced": "Give enough detail to act on, and no more.",
        "thorough": "Be thorough: include the reasoning, the evidence and the edge cases.",
    },
    "style": {
        "plain": "Use plain language without jargon, and explain any technical term you need.",
        "technical": "Be technically precise: name the files, functions, services and errors involved.",
        "business": "Speak to impact (customers, deadlines, decisions) rather than implementation details.",
    },
    "format": {
        "bullets": "Prefer bullet points.",
        "paragraphs": "Prefer short paragraphs to bullet points.",
        "summary_first": "Start with a one line summary, then the details.",
    },
}
FOCUS_TEXT: dict[str, str] = {
    "risks": "risks",
    "next_steps": "next steps",
    "owners": "who owns what",
    "dates": "dates and deadlines",
    "code": "the code involved",
    "customer_impact": "customer impact",
    "metrics": "numbers and metrics",
}


class Profile(BaseModel):
    """How the agent answers a role's holders. Every field is optional: unset means no preference."""

    model_config = ConfigDict(extra="forbid")

    detail: Detail | None = None
    style: Style | None = None
    format: Format | None = None
    focus: list[Focus] = Field(default_factory=list, max_length=len(FOCUS_TEXT))

    @field_validator("focus")
    @classmethod
    def _unique(cls, value: list[Focus]) -> list[Focus]:
        return list(dict.fromkeys(value))


def profile_of(raw: Any) -> Profile:
    """A stored profile, read leniently: what no longer validates is dropped, never an error."""
    if not isinstance(raw, dict):
        return Profile()
    clean: dict[str, Any] = {}
    for key, options in SENTENCES.items():
        if raw.get(key) in options:
            clean[key] = raw[key]
    clean["focus"] = [f for f in raw.get("focus") or [] if f in FOCUS_TEXT]
    return Profile.model_validate(clean)


def guidance(profile: Profile) -> str:
    """The profile as sentences for the prompt, in a fixed order."""
    parts = [SENTENCES[key][value] for key, value in (("detail", profile.detail), ("style", profile.style),
             ("format", profile.format)) if value]  # fmt: skip
    if profile.focus:
        items = [FOCUS_TEXT[f] for f in profile.focus]
        listed = items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
        parts.append(f"Call out {listed}.")
    return " ".join(parts)


@dataclass(frozen=True)
class RoleLimits:
    """The requester's limits from all their active roles, with the role behind each one (named in
    what the requester is told)."""

    playbooks: frozenset[str] | None = None  # None: every playbook
    playbooks_by: tuple[str, ...] = ()
    blocked: dict[str, str] = field(default_factory=dict)  # "adapter.operation" -> role title
    ask: dict[str, str] = field(default_factory=dict)
    capped_by: str | None = None  # a role that keeps its holders in Ask

    def allows(self, playbook: str) -> bool:
        return self.playbooks is None or playbook in self.playbooks


NONE = RoleLimits()

LIMITS_SQL = (
    "select r.title, r.allowed_playbooks, r.blocked_operations, r.ask_operations, r.max_mode::text "
    "from app.membership_roles mr join app.custom_roles r on r.id = mr.role_id "
    "where mr.membership_id = %s and r.status = 'active' order by mr.is_primary desc, lower(r.title)"
)


def combine(rows: list[tuple[Any, ...]]) -> RoleLimits:
    """Most restrictive wins: playbooks intersect, blocked and ask lists add up, any cap applies."""
    playbooks: frozenset[str] | None = None
    by: list[str] = []
    blocked: dict[str, str] = {}
    ask: dict[str, str] = {}
    capped_by = None
    for title, allowed, blocked_ops, ask_ops, max_mode in rows:
        if allowed is not None:
            playbooks = frozenset(allowed) if playbooks is None else playbooks & frozenset(allowed)
            by.append(str(title))
        for op in blocked_ops or []:
            blocked.setdefault(str(op), str(title))
        for op in ask_ops or []:
            ask.setdefault(str(op), str(title))
        if max_mode == "ask" and capped_by is None:
            capped_by = str(title)
    return RoleLimits(playbooks, tuple(by), blocked, ask, capped_by)


async def limits_for(conn: Any, membership_id: Any) -> RoleLimits:
    """The requester's limits, read in the caller's tenant transaction."""
    rows = await (await conn.execute(LIMITS_SQL, (membership_id,))).fetchall()
    return combine([tuple(r) for r in rows])


async def approver_for(conn: Any, operation: str) -> tuple[Any, str] | None:
    """The role whose holders decide approval requests for ``operation`` (``adapter.operation``):
    the oldest active one that lists it, or None (the operation's own rule applies)."""
    row = await (
        await conn.execute(
            "select id, title from app.custom_roles where status = 'active' "
            "and %s = any(approves_operations) order by created_at, id limit 1",
            (operation,),
        )
    ).fetchone()
    return (row[0], str(row[1])) if row else None


def names(titles: tuple[str, ...] | list[str]) -> str:
    """Role titles for a sentence: "Support", "Support and QA"."""
    items = list(dict.fromkeys(titles))
    if len(items) <= 1:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " and " + items[-1]

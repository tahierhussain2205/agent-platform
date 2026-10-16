"""Validating a schedule against its owner's access (spec 4.6.1): anyone can schedule reports, but
only over channels and tools they can use themselves and to destinations they can post to. The
console and the Slack confirmation both save schedules through here."""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.schedules.cron import Cadence, ScheduleError, cadence

KINDS = {"channel_summary": "report_channel_summary", "jira_progress": "report_jira_progress"}
KIND_OF = {v: k for k, v in KINDS.items()}
TITLES = {"channel_summary": "Channel summary", "jira_progress": "Jira progress"}
PROJECT = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
MAX_CHANNELS = 5


@dataclass
class Draft:
    name: str
    kind: str
    cron: str
    timezone: str
    delivery: str  # dm | channel
    channels: list[UUID] = field(default_factory=list)
    project: str | None = None
    delivery_channel_id: UUID | None = None


@dataclass
class Valid:
    name: str
    playbook_key: str
    params: dict[str, Any]
    cadence: Cadence
    delivery: str
    delivery_channel_id: UUID | None
    instruction: str


async def validate(conn: Any, owner: UUID, draft: Draft, now: datetime | None = None) -> Valid:
    """Raises ``ScheduleError`` with a message for the person saving it."""
    name = " ".join(draft.name.split())
    if not 1 <= len(name) <= 120:
        raise ScheduleError("bad_name", "Give the report a name of 1 to 120 characters.")
    if draft.kind not in KINDS:
        raise ScheduleError("bad_kind", "Pick a report: a channel summary or a Jira progress report.")
    when = cadence(draft.cron, draft.timezone, now or datetime.now(UTC))
    owner_row = await (
        await conn.execute(
            "select m.status, exists (select 1 from app.platform_identities pi join app.workspaces w "
            "on w.id = pi.workspace_id and w.status = 'active' where pi.membership_id = m.id) "
            "from app.org_memberships m where m.id = %s",
            (owner,),
        )
    ).fetchone()
    if owner_row is None or owner_row[0] != "active":
        raise ScheduleError("owner_inactive", "The owner must be an active member.")
    if not owner_row[1]:
        raise ScheduleError(
            "no_slack_account",
            "The owner needs a Slack account in the connected workspace: say hello to the agent in Slack "
            "first.",
        )
    params: dict[str, Any]
    if draft.kind == "channel_summary":
        ids = list(dict.fromkeys(draft.channels))
        if not 1 <= len(ids) <= MAX_CHANNELS:
            raise ScheduleError("bad_channels", f"Pick 1 to {MAX_CHANNELS} channels to summarize.")
        rows = await (
            await conn.execute(
                "select id, name, app.can_read_channel(%s, id) from app.channels where id = any(%s)",
                (owner, ids),
            )
        ).fetchall()
        found = {r[0]: r for r in rows}
        for channel_id in ids:
            row = found.get(channel_id)
            if row is None:
                raise ScheduleError("bad_channels", "One of the channels does not exist.")
            if not row[2]:
                raise ScheduleError(
                    "channel_access",
                    f"The owner can't read #{row[1]} through the agent (allowed and a member).",
                )
        params = {"channels": [str(i) for i in ids]}
        about = ", ".join(f"#{found[i][1]}" for i in ids)
    else:
        project = (draft.project or "").strip().upper()
        if not PROJECT.match(project):
            raise ScheduleError("bad_project", "Give a Jira project key, like PAY.")
        enabled = await (
            await conn.execute(
                "select enabled from app.org_integrations where integration_key = 'jira'",
            )
        ).fetchone()
        if not enabled or not enabled[0]:
            raise ScheduleError("jira_disabled", "Jira is not enabled for this organization.")
        params = {"project": project}
        about = f"Jira {project}"
    delivery_channel = None
    if draft.delivery == "channel":
        if draft.delivery_channel_id is None:
            raise ScheduleError("bad_delivery", "Pick the channel the report goes to.")
        row = await (
            await conn.execute(
                "select name, is_allowlisted and not archived, bot_is_member, app.can_read_channel(%s, id) "
                "from app.channels where id = %s",
                (owner, draft.delivery_channel_id),
            )
        ).fetchone()
        if row is None:
            raise ScheduleError("bad_delivery", "That channel does not exist.")
        if not row[1]:
            raise ScheduleError("delivery_not_allowed", f"The agent is not allowed in #{row[0]}.")
        if not row[2]:
            raise ScheduleError(
                "delivery_bot_missing", f"Invite the agent to #{row[0]} first (/invite @agent)."
            )
        if not row[3]:
            raise ScheduleError("delivery_access", f"The owner is not a member of #{row[0]}.")
        delivery_channel = draft.delivery_channel_id
    elif draft.delivery != "dm":
        raise ScheduleError("bad_delivery", "Deliver to a channel or by direct message.")
    instruction = f"{TITLES[draft.kind]} of {about}, {when.describe().lower()} ({when.timezone})"
    return Valid(name, KINDS[draft.kind], params, when, draft.delivery, delivery_channel, instruction)

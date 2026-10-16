"""GitHub feedback in the thread (plan phase 12): a review, failed checks or the end of a pull request
the agent opened is posted in the thread of the task that opened it.

The details are read within the requester's own access (``github.get_pull_request`` through the
``ToolExecutor``), and the notice passes the output policy like any answer: when not everyone in
the thread may see the repository, the thread gets a notice without the review's text. Each
delivery is posted once (``post_once`` keyed by the delivery id). Nothing is started: the engineer
asks for a revision in the thread.
"""

from typing import TYPE_CHECKING, Any

from app.agent.implementation import FAILED_STATES
from app.agent.outbox import post_once
from app.agent.types import GitHubNotice, TaskRef
from app.db.tenant import tenant_tx
from app.policy import Destination
from app.policy.redaction import redact
from app.slack.adapter import Reply, escape
from app.tools.base import RefBook, Source, tool_name

if TYPE_CHECKING:
    from app.agent.runner import AgentRunner

STATES = {"CHANGES_REQUESTED": "requested changes on", "APPROVED": "approved", "COMMENTED": "commented on"}


async def task_ref(runner: "AgentRunner", org_id: str, task_id: str) -> TaskRef | None:
    """The task's conversation, rebuilt from its rows (its thread, its requester)."""
    async with tenant_tx(runner.d.pool, org_id) as conn:
        row = await (
            await conn.execute(
                "select t.session_id, s.workspace_id, w.external_id, s.external_channel_id, s.channel_id, "
                "s.external_thread_id, t.requested_by, pi.external_user_id, t.trigger "
                "from app.tasks t join app.sessions s on s.id = t.session_id "
                "join app.workspaces w on w.id = s.workspace_id "
                "left join app.platform_identities pi on pi.membership_id = t.requested_by "
                "and pi.workspace_id = s.workspace_id where t.id = %s",
                (task_id,),
            )
        ).fetchone()
    if row is None or row[6] is None:
        return None
    channel = str(row[3])
    kind = "im" if channel.startswith("D") else "group" if channel.startswith("G") else "channel"
    return TaskRef(
        org_id, task_id, str(row[0]), str(row[1]), str(row[2]), channel, kind,
        str(row[4]) if row[4] else None, str(row[5]), str(row[5]), str(row[6]), str(row[7] or ""),
        str(row[8]),
    )  # fmt: skip


async def post_notice(runner: "AgentRunner", notice: GitHubNotice) -> str:
    """Posts the notice in the task's thread; returns posted, private (no details) or gone."""
    ref = await task_ref(runner, notice.org_id, notice.task_id)
    if ref is None:
        return "gone"
    ctx = await runner._context(ref)
    _, _, policy, executor = await runner._tools(ref, ctx, "ask", RefBook("P"))
    # The task has ended, and the vault releases credentials to running tasks only: the notice reads
    # outside any task, with the org connection, still checking the requester's own access.
    executor.ctx.task_id = None
    label = f"{notice.repo}#{notice.number}"
    args: dict[str, Any] = {"repo": notice.repo, "number": notice.number}
    if notice.kind == "checks_failed" and notice.sha:
        args["sha"] = notice.sha  # the commit the suite ran on, whatever the head is now
    got = await executor.run(tool_name("github", "get_pull_request"), args)
    pr: dict[str, Any] = got.data if not (got.is_error or got.denied) else {}
    url = pr.get("url")
    link = f"<{url}|{escape(label)}>" if url else escape(label)
    headline, details = _headline(notice, link), _details(notice, pr)
    private = bool(pr.get("private", True))
    source = Source(
        ref=f"github:{notice.repo}#{notice.number}", channel_external_id="", channel_name=notice.repo,
        visibility="restricted" if private else "public", ts="", label=label, permalink=url, kind="github",
        audience=f"github:{notice.repo}",
    )  # fmt: skip
    destination = Destination("thread", ref.channel, None, ctx.channel_name)
    decision = await policy.check_output(destination, [source], audience=executor.audience)
    shown = "posted"
    if not pr:
        details, shown = ["I can't read it with your access now."], "unreadable"
    elif decision.deliver != "as_is":
        details, shown = (
            ["Open the pull request to read it (not everyone here can see the repository)."],
            "private",
        )
    hint = _hint(notice)
    text = "\n".join([headline, *details, *([hint] if hint else [])])
    await post_once(
        runner.d.pool, await runner._sink(ref), ref.org_id, ref.task_id,
        f"github:{notice.delivery}:{notice.number}",
        Reply(ref.channel, redact(text)[0], ref.thread_ts), channel=ref.channel, thread_ts=ref.thread_ts,
    )  # fmt: skip
    return shown


def _headline(notice: GitHubNotice, link: str) -> str:
    who = escape(notice.sender or "Someone")
    if notice.kind == "review":
        return f"💬 {who} {STATES.get(str(notice.state).upper(), 'reviewed')} {link}."
    if notice.kind == "checks_failed":
        at = f" at `{notice.sha[:7]}`" if notice.sha else ""
        return f"❌ Checks failed on {link}{at} ({escape(str(notice.state))})."
    return f"🎉 {link} was merged." if notice.state == "merged" else f"{link} was closed without merging."


def _details(notice: GitHubNotice, pr: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if notice.kind == "review":
        review = next((r for r in pr.get("reviews") or [] if r.get("id") == notice.review_id), None)
        if review and review.get("body"):
            lines.append(f"> {escape(str(review['body']))[:600]}")
        for c in [c for c in pr.get("comments") or [] if c.get("review") == notice.review_id][:5]:
            where = f"`{escape(str(c.get('path')))}:{c.get('line')}`" if c.get("path") else ""
            lines.append(f"• {where} {escape(str(c.get('body') or ''))[:300]}".replace("•  ", "• "))
    elif notice.kind == "checks_failed":
        for c in [c for c in pr.get("checks") or [] if c.get("conclusion") in FAILED_STATES][:5]:
            lines.append(
                f"• {escape(str(c.get('name')))}: "
                + escape(str(c.get("summary") or c.get("conclusion")))[:300]
            )
    return lines


def _hint(notice: GitHubNotice) -> str | None:
    if notice.kind == "review" and str(notice.state).upper() != "APPROVED":
        return '_Ask me here to "address the review comments" and I will revise the pull request._'
    if notice.kind == "checks_failed":
        return '_Ask me here to "fix CI" and I will look at the failure and revise the pull request._'
    return None

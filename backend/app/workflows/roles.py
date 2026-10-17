"""Role previews (D-202): one request about a sample conversation, answered without any role and as
the role an admin is editing, so the console can show the difference a profile makes before it is
saved. The conversation and the samples are ours; nothing is stored and no tool is offered."""

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.agent import prompts
    from app.agent.roles import profile_of
    from app.db.tenant import tenant_tx
    from app.llm.gateway import BudgetExceededError, CallContext

SAMPLE_THREAD = "\n".join(
    [
        "Priya (Support): Three customers wrote in this morning: their order total is wrong when the cart "
        "has more than one line. Only the first item seems to be charged.",
        "Marco (Backend): Confirmed in cart-service. The total stops after the first line; it looks like it "
        "came in with Tuesday's refactor of the pricing module.",
        "Priya (Support): Also, someone used a 150% discount code yesterday and the order total came out "
        "negative.",
        "Dana (Engineering manager): How many customers are affected? Do we need to pause the spring promo?",
        "Marco (Backend): About 40 orders since Tuesday are undercharged. Both fixes are small: count every "
        "line, and refuse discounts over 100%. I can open a pull request today with tests.",
        "Sam (Finance): We will need the list of affected orders to reconcile before month end on the 31st.",
    ]
)

SAMPLE_REQUESTS = {
    "summary": "Summarize this thread for me.",
    "status": "Where do things stand, and what should happen next?",
    "fix": "How should we fix this?",
}


@dataclass
class PreviewRequest:
    org_id: str
    membership_id: str
    title: str
    description: str
    profile: dict[str, Any] = field(default_factory=dict)
    request: str = SAMPLE_REQUESTS["summary"]


@dataclass
class PreviewResult:
    without: str
    with_role: str
    stopped: str | None = None


class PreviewActivities:
    def __init__(self, deps: Any) -> None:
        self.d = deps  # AgentDeps: the pool and the gateway

    @activity.defn(name="role_preview")
    async def preview(self, req: PreviewRequest) -> PreviewResult:
        async with tenant_tx(self.d.pool, req.org_id) as conn:
            row = await (
                await conn.execute("select name from app.orgs where id = %s", (req.org_id,))
            ).fetchone()
        org = str(row[0]) if row else "the organization"
        role = prompts.RoleInfo(req.title, req.description, True, profile_of(req.profile))
        try:
            without, with_role = await asyncio.gather(
                self._answer(req, org, []), self._answer(req, org, [role])
            )
        except BudgetExceededError as exc:
            return PreviewResult("", "", f"No preview: {exc}.")
        return PreviewResult(without, with_role)

    async def _answer(self, req: PreviewRequest, org: str, roles: list[prompts.RoleInfo]) -> str:
        request = " ".join(req.request.replace("<", "").replace(">", "").split())[:300]
        result = await self.d.gateway.complete(
            CallContext(UUID(req.org_id), UUID(req.membership_id), None, "role_preview"),
            "fast",
            system=prompts.system(org, None, roles, prompts.PREVIEW),
            messages=[
                {
                    "role": "user",
                    "content": f"<thread>\n{SAMPLE_THREAD}\n</thread>\n<request>{request}</request>",
                }
            ],
            tools=[prompts.submit_tool("submit_preview", "Submit the answer.")],
            tool_choice={"type": "tool", "name": "submit_preview"},
            max_tokens=900,
        )
        return str(result.tool_input("submit_preview").get("answer") or "").strip()[:4000]


@workflow.defn(name="RolePreviewWorkflow")
class RolePreviewWorkflow:
    @workflow.run
    async def run(self, req: PreviewRequest) -> PreviewResult:
        result: PreviewResult = await workflow.execute_activity_method(
            PreviewActivities.preview,
            req,
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        return result

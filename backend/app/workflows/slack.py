"""Slack workflows. Ingress starts them; workers run the activities."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.workflows.slack_activities import SlackActivities
    from app.workflows.slack_types import HomeRequest, SlackRequest, SyncRequest, SyncResult

RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1), maximum_interval=timedelta(seconds=30), maximum_attempts=6
)
SHORT = timedelta(seconds=60)


@workflow.defn(name="AgentRequestWorkflow")
class AgentRequestWorkflow:
    """A mention or DM. Phase 6 stub: identify, check access, acknowledge in the thread. The agent
    loop replaces the acknowledgement in phase 7."""

    @workflow.run
    async def run(self, req: SlackRequest) -> str:
        requester = await workflow.execute_activity_method(
            SlackActivities.identify_requester, req, start_to_close_timeout=SHORT, retry_policy=RETRY
        )
        if requester.membership_id is None:
            await workflow.execute_activity_method(
                SlackActivities.request_access,
                args=[req, requester],
                start_to_close_timeout=SHORT,
                retry_policy=RETRY,
            )
            return "access_requested"
        check = await workflow.execute_activity_method(
            SlackActivities.check_channel,
            args=[req, requester],
            start_to_close_timeout=SHORT,
            retry_policy=RETRY,
        )
        if not check.allowed:
            await workflow.execute_activity_method(
                SlackActivities.explain_refusal,
                args=[req, check],
                start_to_close_timeout=SHORT,
                retry_policy=RETRY,
            )
            return check.reason
        await workflow.execute_activity_method(
            SlackActivities.acknowledge,
            args=[req, requester],
            start_to_close_timeout=SHORT,
            retry_policy=RETRY,
        )
        return "replied"


@workflow.defn(name="SyncWorkspaceWorkflow")
class SyncWorkspaceWorkflow:
    """Channels and members of allowlisted channels, from Slack into Postgres and the cache."""

    @workflow.run
    async def run(self, req: SyncRequest) -> SyncResult:
        return await workflow.execute_activity_method(
            SlackActivities.sync_workspace,
            req,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RETRY,
        )


@workflow.defn(name="PublishHomeWorkflow")
class PublishHomeWorkflow:
    @workflow.run
    async def run(self, req: HomeRequest) -> str:
        return await workflow.execute_activity_method(
            SlackActivities.publish_home, req, start_to_close_timeout=SHORT, retry_policy=RETRY
        )

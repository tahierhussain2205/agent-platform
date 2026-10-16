"""GitHub feedback (plan phase 12): one workflow per webhook delivery and pull request posts the
notice in the thread of the task that opened the pull request (``app.agent.notices``)."""

from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.agent.notices import post_notice
    from app.agent.runner import AgentRunner
    from app.agent.types import GitHubNotice


@workflow.defn(name="GitHubNoticeWorkflow")
class GitHubNoticeWorkflow:
    @workflow.run
    async def run(self, notice: GitHubNotice) -> str:
        result: str = await workflow.execute_activity(
            "github_notice",
            notice,
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=5, initial_interval=timedelta(seconds=2)),
        )
        return result


class GitHubActivities:
    def __init__(self, runner: AgentRunner) -> None:
        self.runner = runner

    @activity.defn(name="github_notice")
    async def notice(self, notice: GitHubNotice) -> str:
        return await post_notice(self.runner, notice)

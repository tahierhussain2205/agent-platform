"""A trivial workflow used by smoke tests to prove a worker is connected and polling."""

from datetime import timedelta

from temporalio import activity, workflow


@activity.defn(name="ping")
async def ping(message: str) -> str:
    return f"pong: {message}"


@workflow.defn(name="PingWorkflow")
class PingWorkflow:
    @workflow.run
    async def run(self, message: str) -> str:
        return await workflow.execute_activity(ping, message, start_to_close_timeout=timedelta(seconds=10))

"""Role previews on the worker (D-202): the same request answered without any role and as the role,
the profile visibly changing the answer, metered to the org with no task."""

from app.workflows.roles import SAMPLE_REQUESTS, PreviewActivities, PreviewRequest
from tests.agent.conftest import Agent, model_requests, reset_model


async def test_a_preview_answers_without_and_with_the_role(agent: Agent) -> None:
    await reset_model()
    acme = agent.world.acme
    preview = PreviewActivities(agent.runner.d)
    result = await preview.preview(
        PreviewRequest(
            str(acme.id), str(acme.admin), "Support", "Customers first.",
            {"detail": "brief", "focus": ["risks", "next_steps"], "unknown": "dropped"},
            SAMPLE_REQUESTS["summary"],
        )
    )  # fmt: skip
    assert result.stopped is None
    assert result.without.count("•") == 3
    assert result.with_role.count("•") == 2  # brief
    assert result.with_role.endswith("*Also:* risks and next steps.")

    systems = [str(r["system"]) for r in await model_requests()]
    assert len(systems) == 2
    assert any("No roles are set" in s for s in systems)
    assert any("Primary role: Support. Keep answers short" in s and "Customers first." in s for s in systems)
    metered = agent.sql(
        "select count(*), count(task_id) from app.usage_events where org_id = %s and purpose = 'role_preview'",
        (acme.id,),
    )
    assert metered[0] == (2, 0)

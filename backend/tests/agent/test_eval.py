"""The eval harness: case loading, scoring, and every built in case run against the stand-ins."""

import json
from pathlib import Path

import pytest

from app import cli
from app.eval import CaseResult, EvalRun, load_cases, score, summarize
from tests.agent.conftest import Agent
from tests.connect import (
    connect_github,
    connect_jira,
    connect_logging,
    enable,
    link_github,
    reset_jira_issues,
    sql,
)
from tests.stack import FAKES


def test_built_in_cases_are_valid() -> None:
    cases = load_cases()
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)) >= 6
    for case in cases:
        assert case["expect"], case["id"]
        assert case.get("description"), case["id"]


def test_case_files_must_have_the_required_fields(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text(json.dumps({"id": "x", "request": "hi"}))
    with pytest.raises(ValueError, match="missing"):
        load_cases(tmp_path)


def test_scoring() -> None:
    case = {
        "expect": {
            "playbook": "slack_qa", "channels": ["bugs"], "window": "today", "answer_includes": ["500", "v41"],
            "answer_excludes": ["reorg"], "cites_setup": True, "refused": False, "delivered": "thread",
        },
        "max_tokens": 100,
    }  # fmt: skip
    perfect = {
        "playbook": "slack_qa", "channels": ["bugs"], "window": "today", "answer": "a 500, fixed in V41",
        "setup_ts": ["1.1", "1.2"], "cited_ts": ["1.1", "1.2", "1.3"], "refused": False, "delivered": "thread",
        "tokens": 90,
    }  # fmt: skip
    assert set(score(case, perfect).values()) == {1.0}
    worse = {
        **perfect,
        "answer": "500 and a reorg",
        "cited_ts": ["1.1"],
        "delivered": "private",
        "tokens": 200,
    }
    checks = score(case, worse)
    assert checks["answer_includes"] == 0.5
    assert checks["answer_excludes"] == 0.0
    assert checks["retrieval_recall"] == 0.5
    assert checks["delivery"] == 0.0
    assert checks["token_budget"] == 0.0


def test_ticket_plans_are_scored_after_the_checks_in_code() -> None:
    case = {
        "expect": {
            "tickets": [{"summary_includes": "shipping", "assignee": "bob@acme.test"},
                        {"summary_includes": "Refund", "assignee": "alice@acme.test"}],
            "skips_include": ["PAY-101", "PAY-102"],
        }
    }  # fmt: skip
    ticket = {"operation": "create_issue", "summary": "", "skip": None}
    observed = {
        "actions": [
            {**ticket, "args": {"summary": "Shipping fees are wrong", "assignee_email": "bob@acme.test"}},
            {**ticket, "args": {"summary": "Refund page crashes", "assignee_email": "quinn@acme.test"}},
            {**ticket, "args": {"summary": "Refund page crashes again"}, "skip": "looks like PAY-101"},
        ]
    }
    checks = score(case, observed)
    assert checks["tickets"] == 0.5  # the refund ticket went to someone else
    assert checks["skips"] == 0.5


def test_summary_table() -> None:
    table, mean = summarize(
        [CaseResult("a", 1.0, {"x": 1.0}, 10, 1.0), CaseResult("b", 0.5, {"x": 0.5}, 20, 2.0, notes=["x"]),
         CaseResult("c", 0.0, {}, 0, 0.0, skipped="needs the Slack stand-in")]
    )  # fmt: skip
    assert mean == 0.75
    assert "b" in table
    assert "skip" in table
    assert "(2 scored, 1 skipped)" in table


def test_the_eval_command_is_registered() -> None:
    args = cli.build_parser().parse_args(["eval", "--min-score", "0.9", "--report", "out.json"])
    assert (args.command, args.min_score, args.report) == ("eval", 0.9, "out.json")


async def test_every_built_in_case_passes_against_the_stand_ins(agent: Agent) -> None:
    acme = agent.world.acme
    enable(agent.stack, acme, "jira")  # the ticket and replay cases need Jira, GitHub and logs
    await connect_jira(agent.stack, acme, "acc-alice")
    enable(agent.stack, acme, "github")
    await connect_github(agent.stack, acme)
    await link_github(agent.stack, acme, acme.user, "bob-gh")
    await connect_logging(agent.stack, acme)
    reset_jira_issues()
    run = EvalRun(agent.runner, agent.slack_activities, agent.stack.pool, f"{FAKES}/slack")
    results = [await run.run_case(case) for case in load_cases()]
    failing = {r.id: {"checks": r.checks, "skipped": r.skipped, "notes": r.notes} for r in results
               if r.score < 1 or r.skipped}  # fmt: skip
    assert not failing, json.dumps(failing)
    # Eval tasks close themselves: nothing is left running to block the org.
    open_tasks = agent.sql(
        "select count(*) from app.tasks where org_id = %s and status in ('queued', 'running')",
        (agent.world.acme.id,),
    )
    assert open_tasks == [(0,)]


async def test_cases_that_need_seeding_are_skipped_against_real_slack(agent: Agent) -> None:
    run = EvalRun(agent.runner, agent.slack_activities, agent.stack.pool, None)
    case = next(c for c in load_cases() if c.get("setup"))
    result = await run.run_case(case)
    assert result.skipped == "needs the Slack stand-in to seed messages"


async def test_cases_that_need_an_integration_are_skipped_without_it(agent: Agent) -> None:
    sql(agent.stack, "update app.org_integrations set enabled = false where integration_key = 'jira'")
    run = EvalRun(agent.runner, agent.slack_activities, agent.stack.pool, f"{FAKES}/slack")
    case = next(c for c in load_cases() if c.get("requires"))
    result = await run.run_case(case)
    assert result.skipped == "needs jira connected for the org"

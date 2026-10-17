"""Tool inputs put right by their schema: arrays and objects written as JSON strings, the model's call
markup left in strings, scalars written as text. Anything else is left exactly as it came."""

import json
from typing import Any

from app.agent.prompts import submit_tool
from app.llm.repair import cut_markup, repair_content

PLAN = submit_tool("submit_plan", "Submit the plan.")
REPORT = submit_tool("submit_report", "Submit the answer.")
ACTION = {"tool": "jira", "operation": "create_issue", "args": {"project": "KAN"}, "summary": "Ticket"}


def call(name: str, value: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"type": "text", "text": "</invoke>"},
        {"type": "tool_use", "id": "t1", "name": name, "input": value},
    ]


def repaired(name: str, value: dict[str, Any], tool: dict[str, Any] = PLAN) -> tuple[Any, list[str]]:
    content, fixed = repair_content(call(name, value), [tool])
    assert content[0] == {"type": "text", "text": "</invoke>"}  # text blocks are not tool input
    return content[1]["input"], fixed


def test_an_array_written_as_json_is_decoded_up_to_the_markup_after_it() -> None:
    written = json.dumps([ACTION]) + '\n</parameter>\n<parameter name="notes">One ticket'
    value, fixed = repaired("submit_plan", {"actions": written})
    assert value == {"actions": [ACTION]}
    assert fixed == ["submit_plan.actions"]


def test_one_object_where_a_list_belongs_becomes_a_list() -> None:
    assert repaired("submit_plan", {"actions": json.dumps(ACTION)})[0] == {"actions": [ACTION]}
    assert repaired("submit_plan", {"actions": ACTION})[0] == {"actions": [ACTION]}


def test_the_call_markup_is_cut_from_strings_at_any_depth() -> None:
    answer = "No open questions were found.</answer>\n</invoke>"
    value, fixed = repaired("submit_report", {"answer": answer, "confidence": "high"}, REPORT)
    assert value == {"answer": "No open questions were found.", "confidence": "high"}
    assert fixed == ["submit_report.answer"]
    action = {**ACTION, "args": {"summary": "Add a test</parameter>", "labels": ["a</invoke>"]}}
    value, fixed = repaired("submit_plan", {"actions": [action]})
    assert value["actions"][0]["args"] == {"summary": "Add a test", "labels": ["a"]}
    assert fixed == ["submit_plan.actions[].args.summary", "submit_plan.actions[].args.labels[]"]


def test_scalars_written_as_text_are_read() -> None:
    tool = {"name": "t", "input_schema": {"type": "object", "properties": {
        "n": {"type": "integer"}, "x": {"type": "number"}, "ok": {"type": "boolean"}, "no": {"type": "boolean"},
    }}}  # fmt: skip
    value, fixed = repaired("t", {"n": "3", "x": " 0.5</parameter>", "ok": "True", "no": "maybe"}, tool)
    assert value == {"n": 3, "x": 0.5, "ok": True, "no": "maybe"}
    assert fixed == ["t.n", "t.x", "t.ok"]


def test_what_is_already_right_or_cannot_be_read_is_left_as_it_came() -> None:
    answer = "Use <https://x.test|the runbook> when a < b; the template ends with </div>. [1]"
    good = {"answer": answer, "confidence": "high", "not_verified": []}
    assert repaired("submit_report", good, REPORT) == (good, [])
    assert repaired("submit_plan", {"actions": [ACTION], "notes": "fine"}) == (
        {"actions": [ACTION], "notes": "fine"},
        [],
    )
    for unreadable in ("[{'tool': 'jira'", "not json", '"a string"'):
        assert repaired("submit_plan", {"actions": unreadable}) == ({"actions": unreadable}, [])
    other = call("jira_search", {"jql": "x</invoke>"})
    assert repair_content(other, [PLAN]) == (other, [])  # a tool we did not offer: untouched


def test_cut_markup() -> None:
    assert cut_markup("done.</answer>", "answer") == "done."
    assert cut_markup("done.</answer>") == "done.</answer>"  # another field's tag is content
    assert cut_markup('x <parameter name="y">z') == "x"
    assert cut_markup("plain") == "plain"


def test_fields_put_under_a_key_of_the_models_own_are_unwrapped() -> None:
    """Demo run 4: a plan came back with nothing the schema knows at its top level."""
    tool = submit_tool("submit_investigation_plan", "Submit the plan.")
    plan = {"goal": "Count every line", "steps": [{"kind": "implement", "text": "Fix it"}]}
    value, fixed = repaired("submit_investigation_plan", {"plan": plan}, tool)
    assert value == plan
    assert fixed == ["submit_investigation_plan.plan"]
    value, _ = repaired("submit_investigation_plan", {"plan": json.dumps(plan), "summary": "Outside"}, tool)
    assert value == {**plan, "summary": "Outside"}
    for left in ({"plan": {"unrelated": 1}}, {"a": plan, "b": plan}, {"note": "just text"}):
        assert repaired("submit_investigation_plan", left, tool)[0] == left, left

"""The Anthropic stand-in speaks the Messages API shapes the gateway relies on."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fakes.config import Config
from fakes.main import create_app

KEY = {"x-api-key": "sk-ant-local-key", "anthropic-version": "2023-06-01"}
MODEL = "claude-sonnet-5"


@pytest.fixture
def client(tmp_path: Any) -> Iterator[TestClient]:
    config = Config(state_dir=tmp_path, seed_fixture=tmp_path / "none.json")
    with TestClient(create_app(config, persist=False)) as c:
        yield c


def call(client: TestClient, **body: Any) -> Any:
    payload = {"model": MODEL, "max_tokens": 512, "messages": [{"role": "user", "content": "hi"}], **body}
    return client.post("/anthropic/v1/messages", json=payload, headers=KEY)


def forced(name: str) -> dict[str, Any]:
    return {
        "tools": [{"name": name, "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "tool", "name": name},
    }


def tool_input(response: Any) -> Any:
    (block,) = [b for b in response.json()["content"] if b["type"] == "tool_use"]
    return block["input"]


def test_auth_model_and_validation_errors(client: TestClient) -> None:
    bad = client.post("/anthropic/v1/messages", json={}, headers={"x-api-key": "nope"})
    assert (bad.status_code, bad.json()["error"]["type"]) == (401, "authentication_error")
    assert call(client, model="claude-imaginary").json()["error"]["type"] == "not_found_error"
    assert call(client, messages=[]).json()["error"]["type"] == "invalid_request_error"
    assert call(client, max_tokens=0).status_code == 400


def test_response_envelope_and_usage(client: TestClient) -> None:
    body = call(client).json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["stop_reason"] == "end_turn"
    assert body["content"][0]["type"] == "text"
    assert body["usage"]["input_tokens"] > 0
    assert body["usage"]["output_tokens"] > 0


def test_prompt_cache_hits_on_the_same_prefix(client: TestClient) -> None:
    system = [{"type": "text", "text": "rules " * 200, "cache_control": {"type": "ephemeral"}}]
    first = call(client, system=system).json()["usage"]
    second = call(client, system=system).json()["usage"]
    other = call(client, system=[{**system[0], "text": "other " * 200}]).json()["usage"]
    assert first["cache_creation_input_tokens"] > 0
    assert first["cache_read_input_tokens"] == 0
    assert second["cache_read_input_tokens"] == first["cache_creation_input_tokens"]
    assert other["cache_read_input_tokens"] == 0  # a different prefix is a miss
    assert call(client).json()["usage"]["cache_read_input_tokens"] == 0  # no breakpoint, no caching


def test_classification(client: TestClient) -> None:
    def classify(text: str) -> Any:
        return tool_input(
            call(
                client,
                messages=[{"role": "user", "content": f"<request>{text}</request>"}],
                **forced("submit_classification"),
            )
        )

    assert classify("@agent summarize today's discussion in #bugs")["playbook"] == "slack_qa"
    unclear = classify("@agent hello")
    assert unclear["playbook"] == "none"
    assert unclear["clarifying_question"]
    assert classify("maybe summarize #bugs?")["confidence"] < 0.6


def test_brief_extracts_channels_window_and_ids(client: TestClient) -> None:
    now = datetime(2026, 10, 16, 15, 30, tzinfo=UTC)
    user = (
        "<thread>\n[B1] (2026-10-16 15:29 UTC) Bob Builder: @agent summarize today's discussion in #bugs "
        "and #general about PAY-12</thread>\n<request_ref>B1</request_ref>\n"
        f"<now>{now.isoformat()}</now>"
    )
    brief = tool_input(call(client, messages=[{"role": "user", "content": user}], **forced("submit_brief")))
    assert brief["entities"]["channels"] == ["bugs", "general"]
    assert brief["entities"]["ids"] == ["PAY-12"]
    assert brief["entities"]["time_window"]["start"] == "2026-10-16T00:00:00+00:00"
    assert brief["entities"]["time_window"]["label"] == "today"
    assert brief["facts"][0]["sources"] == ["B1"]
    assert brief["goal"].startswith("summarize today's discussion")


def test_gather_reads_each_channel_then_submits_findings(client: TestClient) -> None:
    tools = [{"name": n, "input_schema": {"type": "object"}} for n in ("slack_read_channel", "submit_findings")]
    brief = {"entities": {"channels": ["bugs", "leads-private"], "time_window": {"start": "2026-10-16T00:00:00+00:00"}}}
    messages: list[dict[str, Any]] = [{"role": "user", "content": f"<brief>{json.dumps(brief)}</brief>"}]
    first = call(client, messages=messages, tools=tools).json()
    calls = [b for b in first["content"] if b["type"] == "tool_use"]
    assert first["stop_reason"] == "tool_use"
    assert [c["input"]["channel"] for c in calls] == ["bugs", "leads-private"]
    assert calls[0]["input"]["start"] == "2026-10-16T00:00:00+00:00"
    messages += [
        {"role": "assistant", "content": first["content"]},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": calls[0]["id"],
                    "content": "Messages in #bugs (2):\n[S1] (2026-10-16 09:00 UTC) Quinn Tester: Checkout fails\n"
                    "  ↳ [S2] (2026-10-16 09:10 UTC) Bob Builder: Same with 20 items",
                },
                {
                    "type": "tool_result",
                    "tool_use_id": calls[1]["id"],
                    "content": "Access denied: you are not a member of #leads-private.",
                    "is_error": True,
                },
            ],
        },
    ]
    findings = tool_input(call(client, messages=messages, tools=tools))
    assert findings["points"] == [
        {"text": "Quinn Tester: Checkout fails", "sources": ["S1"]},
        {"text": "Bob Builder: Same with 20 items", "sources": ["S2"]},
    ]
    assert findings["gaps"] == ["Access denied: you are not a member of #leads-private."]


def test_gather_reads_the_current_thread_when_no_channel_is_named(client: TestClient) -> None:
    tools = [{"name": n, "input_schema": {"type": "object"}} for n in ("slack_read_thread", "submit_findings")]
    user = '<brief>{"entities": {"channels": []}}</brief>\n<conversation channel="general" thread="1.5"/>'
    response = call(client, messages=[{"role": "user", "content": user}], tools=tools)
    assert tool_input(response) == {"channel": "general", "ts": "1.5"}


def test_plan_and_report(client: TestClient) -> None:
    share = {"goal": "summarize #bugs and share it in #general"}
    planned = tool_input(
        call(
            client,
            messages=[{"role": "user", "content": f"<brief>{json.dumps(share)}</brief>"}],
            **forced("submit_plan"),
        )
    )
    assert planned["actions"][0]["args"] == {"channel": "general", "text": "{answer}"}
    plain = tool_input(
        call(
            client,
            messages=[{"role": "user", "content": '<brief>{"goal": "what broke?"}</brief>'}],
            **forced("submit_plan"),
        )
    )
    assert plain["actions"] == []
    findings = {"points": [{"text": "Checkout fails", "sources": [1]}], "gaps": ["Access denied: #x"]}
    report = tool_input(
        call(
            client,
            messages=[{"role": "user", "content": f"<findings>{json.dumps(findings)}</findings>"}],
            **forced("submit_report"),
        )
    )
    assert "• Checkout fails [1]" in report["answer"]
    assert "Not covered: Access denied: #x" in report["answer"]
    empty = tool_input(
        call(
            client,
            messages=[
                {"role": "user", "content": '<findings>{"points": [], "gaps": ["Access denied: #y"]}</findings>'}
            ],
            **forced("submit_report"),
        )
    )
    assert empty["confidence"] == "low"
    assert "Access denied: #y" in empty["answer"]


def test_faults_scripts_and_recorded_requests(client: TestClient) -> None:
    client.post("/_admin/anthropic/faults", json={"status": 529, "count": 2})
    client.post("/_admin/anthropic/faults", json={"status": 429, "count": 1, "retry_after": 0.5})
    assert call(client).json()["error"]["type"] == "overloaded_error"
    assert call(client).status_code == 529
    limited = call(client)
    assert (limited.status_code, limited.headers["retry-after"]) == (429, "0.5")
    assert call(client).status_code == 200
    client.post(
        "/_admin/anthropic/script",
        json={
            "responses": [[{"type": "tool_use", "name": "slack_read_channel", "input": {"channel": "leads-private"}}]]
        },
    )
    scripted = call(client).json()
    assert scripted["content"][0]["input"] == {"channel": "leads-private"}
    assert scripted["stop_reason"] == "tool_use"
    recorded = client.get("/_admin/anthropic/requests").json()
    assert len(recorded) == 5  # faulted requests are recorded too
    assert recorded[-1]["model"] == MODEL
    client.post("/_admin/reset")
    assert client.get("/_admin/anthropic/requests").json() == []

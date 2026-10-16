"""The LLM gateway: tiers, budgets, caching, retries and fallback, rate limits, metering, tracing."""

import json
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import httpx
import psycopg
import pytest

from app.llm import models
from app.llm.gateway import (
    BudgetExceededError,
    CallContext,
    LLMGateway,
    LLMRefusalError,
    LLMRequestError,
    LLMResponseError,
    LLMUnavailableError,
    Route,
)
from tests.conftest import SPANS
from tests.stack import FAKES, Stack, fakes_up

MODELS = {"fast": "claude-haiku-4-5-20251001", "standard": "claude-sonnet-5", "advanced": "claude-opus-5-5"}
OK = {
    "type": "message",
    "model": "claude-sonnet-5",
    "content": [{"type": "text", "text": "hello"}],
    "stop_reason": "end_turn",
    "usage": {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_creation_input_tokens": 50,
        "cache_read_input_tokens": 400,
    },
}


# ------------------------------------------------------------------ pure parts


@pytest.mark.parametrize(
    ("requested", "allowed", "expected"),
    [
        ("fast", ["fast", "standard", "advanced"], "fast"),
        ("fast", ["standard", "advanced"], "standard"),
        ("advanced", ["fast", "standard"], "standard"),
        ("standard", ["fast", "advanced"], "fast"),  # tie: the cheaper one
        ("advanced", ["advanced"], "advanced"),
    ],
)
def test_resolve_tier(requested: Any, allowed: list[str], expected: str) -> None:
    assert models.resolve_tier(requested, allowed, "standard") == expected


def test_cost_uses_the_price_table() -> None:
    # Sonnet 5: 2 / 10 per MTok, cache write 1.25x, cache read 0.1x (the longest matching prefix wins)
    assert models.cost("claude-sonnet-5", 1_000_000, 0, 0, 0) == Decimal("2.000000")
    assert models.cost("claude-sonnet-5", 0, 1_000_000, 0, 0) == Decimal("10.000000")
    assert models.cost("claude-sonnet-5", 0, 0, 1_000_000, 1_000_000) == Decimal("2.700000")
    assert models.cost("claude-sonnet-4-6", 1_000_000, 0, 0, 0) == Decimal("3.000000")
    # Opus 5.5: 4 / 20, cache reads at their own price
    assert models.cost("claude-opus-5-5", 1_000_000, 1_000_000, 0, 1_000_000) == Decimal("24.200000")
    assert models.cost("claude-opus-5", 1_000_000, 0, 0, 0) == Decimal("5.000000")
    assert models.cost("claude-haiku-4-5-20251001", 1_000_000, 0, 0, 0) == Decimal("1.000000")
    assert models.cost("something-new", 1_000_000, 0, 0, 0) == Decimal("3.000000")


def test_models_that_refuse_forced_tools_are_asked_with_an_instruction() -> None:
    system = [{"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}}]
    forced = {"type": "tool", "name": "submit_plan"}
    tools = [{"name": "submit_plan", "input_schema": {"type": "object"}}]
    body = LLMGateway._body("claude-opus-5-5", system, [], tools, forced, 300, True)
    assert body["tool_choice"] == {"type": "auto"}
    assert body["system"][-1] == {"type": "text", "text": "Answer by calling the submit_plan tool. "
                                                           "Do not answer in plain text."}  # fmt: skip
    assert body["system"][0]["cache_control"] == {
        "type": "ephemeral"
    }  # the instruction is after the cached prefix
    assert LLMGateway._body("claude-opus-5-5", system, [], tools, {"type": "any"}, 300, True)["system"][-1][
        "text"
    ].startswith("Answer by calling one of the tools.")
    # Models that accept forced tool choice keep it; only models that think get room for thinking.
    haiku = LLMGateway._body("claude-haiku-4-5-20251001", system, [], tools, forced, 300, True)
    assert (haiku["tool_choice"], haiku["max_tokens"], len(haiku["system"])) == (forced, 300, 1)
    assert LLMGateway._body("claude-sonnet-5", system, [], tools, forced, 300, True)["tool_choice"] == forced
    for thinking in ("claude-sonnet-5", "claude-opus-5-5", "claude-fable-5-1"):
        assert LLMGateway._body(thinking, system, [], tools, forced, 300, True)["max_tokens"] == 8000
    assert LLMGateway._body("claude-sonnet-5", system, [], tools, forced, 12000, True)["max_tokens"] == 12000


# ------------------------------------------------------------------ against a scripted provider


class Provider:
    """A scripted Messages API: a list of responses (status, body, headers) served in order."""

    def __init__(self, *responses: tuple[int, Any, dict[str, str]]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body, headers = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return httpx.Response(status, json=body, headers=headers)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


def gateway(stack: Stack, provider: Provider, routes: list[Route] | None = None, **kw: Any) -> LLMGateway:
    return LLMGateway(
        routes or [Route("https://primary.test", "key-1")],
        stack.pool,
        stack.kv,
        MODELS,
        http=httpx.AsyncClient(transport=httpx.MockTransport(provider.handler)),
        base_delay=0.001,
        **kw,
    )


def sql(stack: Stack, statement: str, params: tuple[Any, ...] = ()) -> Any:
    with psycopg.connect(stack.env.world.url) as conn:
        cur = conn.execute(statement, params)
        row = cur.fetchone() if cur.description else None
        conn.commit()
        return row


@pytest.fixture
def org(stack: Stack) -> Iterator[Any]:
    """Globex, with its settings restored after each test."""
    o = stack.env.world.globex
    saved = sql(
        stack,
        "select allowed_model_tiers::text[], default_model_tier::text, user_daily_token_limit, org_monthly_token_limit "
        "from app.org_settings where org_id = %s",
        (o.id,),
    )
    yield o
    sql(
        stack,
        "update app.org_settings set allowed_model_tiers = %s::app.model_tier[], default_model_tier = %s, "
        "user_daily_token_limit = %s, org_monthly_token_limit = %s where org_id = %s",
        (*saved, o.id),
    )


def new_task(stack: Stack, org: Any, budget: int | None = None) -> uuid.UUID:
    row = sql(
        stack,
        "insert into app.tasks (org_id, requested_by, trigger, model_tier, token_budget) "
        "values (%s, %s, 'mention', 'standard', %s) returning id",
        (org.id, org.user, budget),
    )
    return row[0]  # type: ignore[no-any-return]


def ctx(org: Any, task: uuid.UUID | None = None, membership: Any = "user", **kw: Any) -> CallContext:
    member = org.user if membership == "user" else membership
    return CallContext(org.id, member, task, "test", **kw)


async def test_request_shape_caching_and_metering(stack: Stack, org: Any) -> None:
    provider = Provider((200, OK, {}))
    task = new_task(stack, org)
    tools = [
        {"name": "a", "input_schema": {"type": "object"}},
        {"name": "b", "input_schema": {"type": "object"}},
    ]
    result = await gateway(stack, provider).complete(
        ctx(org, task),
        "standard",
        system=[{"type": "text", "text": "rules"}, {"type": "text", "text": "playbook"}],
        messages=[{"role": "user", "content": "hi"}],
        tools=tools,
        tool_choice={"type": "tool", "name": "b"},
        max_tokens=300,
    )
    (request,) = provider.requests
    body = json.loads(request.content)
    assert request.url == "https://primary.test/v1/messages"
    assert request.headers["x-api-key"] == "key-1"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert body["model"] == "claude-sonnet-5"
    assert body["max_tokens"] == 8000  # Sonnet 5 thinks by default: room for it beyond the step's 300
    assert "cache_control" not in body["system"][0]
    assert body["system"][1]["cache_control"] == {"type": "ephemeral"}
    assert body["tools"][1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in tools[1]  # the caller's objects are not modified
    assert body["tool_choice"] == {"type": "tool", "name": "b"}
    assert (result.text(), result.usage.counted, result.tier) == ("hello", 170, "standard")
    expected_cost = models.cost("claude-sonnet-5", 100, 20, 50, 400)
    assert result.cost == expected_cost
    event = sql(
        stack,
        "select membership_id, model, input_tokens, output_tokens, cached_tokens, cost from app.usage_events "
        "where task_id = %s",
        (task,),
    )
    assert event == (org.user, "claude-sonnet-5", 150, 20, 400, expected_cost)
    assert sql(stack, "select tokens_used, cost from app.tasks where id = %s", (task,)) == (
        170,
        expected_cost,
    )


async def test_tool_input_helper_requires_the_forced_tool(stack: Stack, org: Any) -> None:
    body = {
        **OK,
        "content": [{"type": "tool_use", "id": "t1", "name": "submit_brief", "input": {"goal": "x"}}],
    }
    result = await gateway(stack, Provider((200, body, {}))).complete(
        ctx(org), "standard", system="s", messages=[{"role": "user", "content": "hi"}]
    )
    assert result.tool_input("submit_brief") == {"goal": "x"}
    with pytest.raises(LLMResponseError):
        result.tool_input("submit_plan")


async def test_tiers_follow_what_the_org_allows(stack: Stack, org: Any) -> None:
    sql(
        stack,
        "update app.org_settings set allowed_model_tiers = '{standard,advanced}', default_model_tier = 'standard' "
        "where org_id = %s",
        (org.id,),
    )
    provider = Provider((200, OK, {}))
    result = await gateway(stack, provider).complete(
        ctx(org), "fast", system="s", messages=[{"role": "user", "content": "x"}]
    )
    assert (result.tier, provider.bodies()[0]["model"]) == ("standard", "claude-sonnet-5")


@pytest.mark.parametrize("scope", ["task", "user_daily", "org_monthly"])
async def test_budgets_refuse_before_calling(stack: Stack, org: Any, scope: str) -> None:
    provider = Provider((200, OK, {}))
    task = new_task(stack, org, budget=1000 if scope == "task" else None)
    if scope == "task":
        sql(stack, "update app.tasks set tokens_used = 1000 where id = %s", (task,))
    else:
        column = "user_daily_token_limit" if scope == "user_daily" else "org_monthly_token_limit"
        sql(stack, f"update app.org_settings set {column} = 10 where org_id = %s", (org.id,))
        sql(
            stack,
            "insert into app.usage_events (org_id, membership_id, kind, input_tokens) values (%s, %s, 'llm', 50)",
            (org.id, org.user if scope == "user_daily" else None),
        )
    with pytest.raises(BudgetExceededError) as info:
        await gateway(stack, provider).complete(
            ctx(org, task), "standard", system="s", messages=[{"role": "user", "content": "x"}]
        )
    assert info.value.scope == scope
    assert provider.requests == []  # refused before the provider was called
    assert "token" in str(info.value)


async def test_retries_then_succeeds(stack: Stack, org: Any) -> None:
    overloaded: tuple[int, Any, dict[str, str]] = (
        529,
        {"type": "error", "error": {"type": "overloaded_error"}},
        {},
    )
    limited = (429, {"type": "error", "error": {"type": "rate_limit_error"}}, {"retry-after": "0"})
    provider = Provider(overloaded, limited, (500, {}, {}), (200, OK, {}))
    result = await gateway(stack, provider).complete(
        ctx(org), "standard", system="s", messages=[{"role": "user", "content": "x"}]
    )
    assert result.text() == "hello"
    assert len(provider.requests) == 4


async def test_fallback_route_after_the_primary_keeps_failing(stack: Stack, org: Any) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "primary.test":
            return httpx.Response(503, json={"type": "error", "error": {"type": "api_error"}})
        return httpx.Response(200, json=OK)

    routes = [Route("https://primary.test", "k1"), Route("https://fallback.test", "k2", "fallback")]
    gw = LLMGateway(
        routes,
        stack.pool,
        stack.kv,
        MODELS,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        base_delay=0.001,
    )
    SPANS.clear()
    await gw.complete(ctx(org), "standard", system="s", messages=[{"role": "user", "content": "x"}])
    assert calls == ["primary.test"] * 4 + ["fallback.test"]
    (span,) = [s for s in SPANS.get_finished_spans() if s.name.startswith("chat ")]
    assert span.attributes is not None
    assert span.attributes["app.llm.route"] == "fallback"


async def test_bad_credentials_skip_to_the_next_route_and_all_failing_routes_raise(
    stack: Stack, org: Any
) -> None:
    provider = Provider((401, {"type": "error", "error": {"type": "authentication_error"}}, {}))
    gw = gateway(
        stack, provider, routes=[Route("https://a.test", "bad"), Route("https://b.test", "bad", "b")]
    )
    with pytest.raises(LLMUnavailableError, match="authentication_error"):
        await gw.complete(ctx(org), "standard", system="s", messages=[{"role": "user", "content": "x"}])
    assert len(provider.requests) == 2  # no retries on a credential error, one try per route


async def test_invalid_requests_are_not_retried(stack: Stack, org: Any) -> None:
    error = {
        "type": "invalid_request_error",
        "message": 'tool_choice: type "tool" and "any" are not supported',
    }
    provider = Provider((400, {"type": "error", "error": error}, {}))
    with pytest.raises(LLMRequestError, match='invalid_request_error: tool_choice: type "tool"'):
        await gateway(stack, provider).complete(
            ctx(org), "standard", system="s", messages=[{"role": "user", "content": "x"}]
        )
    assert len(provider.requests) == 1


async def test_a_refusal_stops_the_step_and_is_metered(stack: Stack, org: Any) -> None:
    refused = {
        **OK,
        "content": [],
        "stop_reason": "refusal",
        "stop_details": {"type": "refusal", "category": "cyber"},
    }
    task = new_task(stack, org)
    with pytest.raises(LLMRefusalError, match=r"declined this request \(cyber\)"):
        await gateway(stack, Provider((200, refused, {}))).complete(
            ctx(org, task), "standard", system="s", messages=[{"role": "user", "content": "x"}]
        )
    assert sql(stack, "select tokens_used > 0 from app.tasks where id = %s", (task,)) == (True,)


async def test_org_rate_limit(stack: Stack, org: Any) -> None:
    gw = gateway(stack, Provider((200, OK, {})), rpm={"interactive": 2, "batch": 1}, max_rate_wait=0.0)

    async def clear() -> None:
        keys = [k async for k in stack.kv.scan_iter(f"ap:t:{org.id}:llm-rpm:*")]
        if keys:
            await stack.kv.delete(*keys)

    async def call(o: Any, lane: Any = "interactive") -> Any:
        return await gw.complete(
            CallContext(o.id, o.user, None, "rl", lane),
            "standard",
            system="s",
            messages=[{"role": "user", "content": "x"}],
        )

    await clear()
    await call(org)
    await call(org)
    with pytest.raises(LLMUnavailableError, match="2 model requests per minute"):
        await call(org)
    await call(org, "batch")  # lanes are counted separately
    with pytest.raises(LLMUnavailableError, match="1 model requests per minute"):
        await call(org, "batch")
    await call(stack.env.world.acme)  # another org is not affected
    await clear()


async def test_spans_carry_usage_but_never_content(stack: Stack, org: Any) -> None:
    SPANS.clear()
    secret_prompt = "the prompt text must never be traced 1f3a"
    await gateway(stack, Provider((200, OK, {}))).complete(
        ctx(org), "standard", system=secret_prompt, messages=[{"role": "user", "content": secret_prompt}]
    )
    (span,) = [s for s in SPANS.get_finished_spans() if s.name.startswith("chat ")]
    attrs = dict(span.attributes or {})
    assert attrs["gen_ai.system"] == "anthropic"
    assert attrs["gen_ai.usage.output_tokens"] == 20
    assert attrs["tenant.id"] == str(org.id)
    assert all(secret_prompt not in str(v) and v != "hello" for v in attrs.values())


@pytest.mark.skipif(not fakes_up(), reason="Anthropic stand-in not reachable (run `make infra`)")
async def test_round_trip_with_the_anthropic_stand_in(stack: Stack, org: Any) -> None:
    gw = LLMGateway([Route(f"{FAKES}/anthropic", "sk-ant-local-key")], stack.pool, stack.kv, MODELS)
    system = [{"type": "text", "text": "Stable rules. " * 100}]
    tool = [{"name": "submit_classification", "input_schema": {"type": "object"}}]
    args: dict[str, Any] = {
        "system": system,
        "messages": [{"role": "user", "content": "<request>summarize #bugs</request>"}],
        "tools": tool,
        "tool_choice": {"type": "tool", "name": "submit_classification"},
    }
    first = await gw.complete(ctx(org), "fast", **args)
    second = await gw.complete(ctx(org), "fast", **args)
    assert first.tool_input("submit_classification")["playbook"] == "slack_qa"
    assert first.model == "claude-haiku-4-5-20251001"
    assert second.usage.cache_read_input_tokens > 0  # the stable prefix was cached
    await gw.aclose()

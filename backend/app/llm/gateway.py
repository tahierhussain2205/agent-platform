"""The LLM gateway (spec 11.3): tier resolution within what the org allows, per tenant rate limits
and token budgets, prompt caching, retries and fallback routes, metering and tracing.

Budgets (spec 4.8) are checked before every call: the task's budget, the requester's daily limit
and the org's monthly limit. ``BudgetExceededError`` tells the caller to stop cleanly; the gateway
never truncates a call half way. Usage is recorded per call in ``usage_events`` (the database
rolls it up into ``usage_daily``) and on the task.

Spans follow the OpenTelemetry GenAI conventions and never carry prompts or completions.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar, Literal
from uuid import UUID

import httpx
from opentelemetry import trace
from psycopg_pool import AsyncConnectionPool

from app.db.tenant import tenant_tx
from app.llm import models
from app.llm.models import Tier

log = logging.getLogger(__name__)
tracer = trace.get_tracer("app.llm")

Lane = Literal["interactive", "batch"]
ANTHROPIC_VERSION = "2023-06-01"
RETRYABLE = {408, 409, 429, 500, 502, 503, 504, 529}


@dataclass(frozen=True)
class Route:
    url: str
    api_key: str
    name: str = "primary"


@dataclass(frozen=True)
class CallContext:
    org_id: UUID
    membership_id: UUID | None
    task_id: UUID | None
    purpose: str
    lane: Lane = "interactive"


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def billable_input(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens

    @property
    def counted(self) -> int:
        """Tokens that count against budgets (the same sum the usage_daily rollup keeps)."""
        return self.billable_input + self.output_tokens


@dataclass(frozen=True)
class LLMResult:
    content: list[dict[str, Any]]
    model: str
    tier: Tier
    stop_reason: str
    usage: Usage
    cost: Decimal

    def text(self) -> str:
        return "\n".join(b.get("text", "") for b in self.content if b.get("type") == "text").strip()

    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    def tool_input(self, name: str) -> dict[str, Any]:
        """The input of the (forced) tool call ``name``."""
        for block in self.tool_uses():
            if block.get("name") == name and isinstance(block.get("input"), dict):
                return block["input"]  # type: ignore[no-any-return]
        raise LLMResponseError(f"the model did not call {name}")


@dataclass(frozen=True)
class BudgetStatus:
    default_tier: str
    allowed_tiers: list[str]
    user_daily_limit: int | None
    user_today: int
    org_monthly_limit: int | None
    org_month: int
    task_budget: int | None
    task_used: int

    def exceeded(self) -> "BudgetExceededError | None":
        if self.task_budget is not None and self.task_used >= self.task_budget:
            return BudgetExceededError("task", self.task_budget, self.task_used)
        if self.user_daily_limit is not None and self.user_today >= self.user_daily_limit:
            return BudgetExceededError("user_daily", self.user_daily_limit, self.user_today)
        if self.org_monthly_limit is not None and self.org_month >= self.org_monthly_limit:
            return BudgetExceededError("org_monthly", self.org_monthly_limit, self.org_month)
        return None


class LLMError(Exception):
    pass


class BudgetExceededError(LLMError):
    MESSAGES: ClassVar[dict[str, str]] = {
        "task": "this task's token budget ({limit:,} tokens) is used up",
        "user_daily": "your daily token limit ({limit:,} tokens) is reached; it resets at 00:00 UTC",
        "org_monthly": "the organization's monthly token limit ({limit:,} tokens) is reached",
    }

    def __init__(self, scope: str, limit: int, used: int) -> None:
        self.scope, self.limit, self.used = scope, limit, used
        super().__init__(self.MESSAGES[scope].format(limit=limit))


class LLMUnavailableError(LLMError):
    """Every route failed after retries."""


class LLMRequestError(LLMError):
    """The provider refused the request itself (a bug on our side); retrying will not help."""


class LLMResponseError(LLMError):
    """The model answered, but not in the shape the step asked for."""


@dataclass
class LLMGateway:
    routes: list[Route]
    pool: AsyncConnectionPool
    kv: Any
    tier_models: dict[str, str]
    rpm: dict[str, int] = field(default_factory=lambda: {"interactive": 120, "batch": 30})
    http: httpx.AsyncClient = field(default_factory=lambda: httpx.AsyncClient(timeout=120.0))
    max_retries: int = 3
    base_delay: float = 0.5
    max_rate_wait: float = 90.0

    # ------------------------------------------------------------------ budgets

    async def budget_status(self, call: CallContext) -> BudgetStatus:
        async with tenant_tx(self.pool, call.org_id) as conn:
            row = await (
                await conn.execute(
                    """
                    select s.default_model_tier::text, s.allowed_model_tiers::text[],
                           s.user_daily_token_limit, s.org_monthly_token_limit,
                           (select coalesce(sum(d.tokens), 0) from app.usage_daily d
                             where d.membership_id = %(m)s and d.day = (now() at time zone 'UTC')::date),
                           (select coalesce(sum(d.tokens), 0) from app.usage_daily d
                             where d.day >= date_trunc('month', now() at time zone 'UTC')::date),
                           t.token_budget, coalesce(t.tokens_used, 0)
                      from app.org_settings s
                      left join app.tasks t on t.id = %(t)s
                    """,
                    {"m": call.membership_id, "t": call.task_id},
                )
            ).fetchone()
        if row is None:
            raise LLMError("org settings not found")
        return BudgetStatus(
            default_tier=row[0],
            allowed_tiers=list(row[1]),
            user_daily_limit=row[2],
            user_today=int(row[4]) if call.membership_id else 0,
            org_monthly_limit=row[3],
            org_month=int(row[5]),
            task_budget=row[6],
            task_used=int(row[7]),
        )

    # ------------------------------------------------------------------ the call

    async def complete(
        self,
        call: CallContext,
        tier: Tier,
        *,
        system: str | list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        max_tokens: int = 2048,
        cache: bool = True,
    ) -> LLMResult:
        status = await self.budget_status(call)
        if over := status.exceeded():
            raise over
        resolved = models.resolve_tier(tier, status.allowed_tiers, status.default_tier)
        model = self.tier_models[resolved]
        await self._rate_limit(call)
        body = self._body(model, system, messages, tools, tool_choice, max_tokens, cache)
        started = time.monotonic()
        with tracer.start_as_current_span(f"chat {model}", kind=trace.SpanKind.CLIENT) as span:
            span.set_attributes(
                {
                    "gen_ai.system": "anthropic",
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": model,
                    "gen_ai.request.max_tokens": max_tokens,
                    "tenant.id": str(call.org_id),
                    "app.llm.purpose": call.purpose,
                    "app.llm.tier": resolved,
                    **({"app.task.id": str(call.task_id)} if call.task_id else {}),
                }
            )
            payload = await self._send(body, span)
            usage = Usage(
                **{
                    k: int(v or 0)
                    for k, v in (payload.get("usage") or {}).items()
                    if k in Usage.__dataclass_fields__
                }
            )
            span.set_attributes(
                {
                    "gen_ai.response.model": payload.get("model", model),
                    "gen_ai.usage.input_tokens": usage.billable_input,
                    "gen_ai.usage.output_tokens": usage.output_tokens,
                    "app.llm.cache_read_tokens": usage.cache_read_input_tokens,
                    "app.llm.stop_reason": str(payload.get("stop_reason")),
                }
            )
        price = models.cost(
            model,
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_creation_input_tokens,
            usage.cache_read_input_tokens,
        )
        result = LLMResult(
            list(payload.get("content") or []), model, resolved, str(payload.get("stop_reason")), usage, price
        )
        await self._meter(call, result)
        log.info(
            "llm call",
            extra={
                "purpose": call.purpose,
                "model": model,
                "input_tokens": usage.billable_input,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_input_tokens,
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return result

    @staticmethod
    def _body(
        model: str,
        system: str | list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: dict[str, Any] | None,
        max_tokens: int,
        cache: bool,
    ) -> dict[str, Any]:
        """Prompt caching: a breakpoint after the stable system prefix and after the tools."""
        blocks: list[dict[str, Any]] = (
            [{"type": "text", "text": system}] if isinstance(system, str) else [dict(b) for b in system]
        )
        if cache and blocks and not any(b.get("cache_control") for b in blocks):
            blocks[-1]["cache_control"] = {"type": "ephemeral"}
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": blocks,
            "messages": messages,
        }
        if tools:
            body["tools"] = [dict(t) for t in tools]
            if cache:
                body["tools"][-1]["cache_control"] = {"type": "ephemeral"}
        if tool_choice:
            body["tool_choice"] = tool_choice
        return body

    async def _send(self, body: dict[str, Any], span: trace.Span) -> dict[str, Any]:
        last_error = "no route configured"
        for route in self.routes:
            for attempt in range(self.max_retries + 1):
                try:
                    response = await self.http.post(
                        f"{route.url.rstrip('/')}/v1/messages",
                        json=body,
                        headers={
                            "x-api-key": route.api_key,
                            "anthropic-version": ANTHROPIC_VERSION,
                            "content-type": "application/json",
                        },
                    )
                except httpx.HTTPError as exc:
                    last_error = f"{route.name}: {type(exc).__name__}"
                    await self._backoff(attempt, None)
                    continue
                if response.status_code == 200:
                    span.set_attribute("app.llm.route", route.name)
                    data: dict[str, Any] = response.json()
                    return data
                kind = _error_type(response)
                last_error = f"{route.name}: {response.status_code} {kind}"
                if response.status_code in (401, 403):
                    log.error("model provider refused our credentials", extra={"route": route.name})
                    break  # this route is misconfigured: try the next one
                if response.status_code not in RETRYABLE:
                    raise LLMRequestError(last_error)
                if attempt < self.max_retries:
                    await self._backoff(attempt, response.headers.get("retry-after"))
            log.warning(
                "model route failed, trying the next one", extra={"route": route.name, "error": last_error}
            )
        span.set_status(trace.Status(trace.StatusCode.ERROR, last_error))
        raise LLMUnavailableError(last_error)

    async def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if retry_after is not None:
            try:
                await asyncio.sleep(min(float(retry_after), 30.0))
                return
            except ValueError:
                pass
        await asyncio.sleep(min(self.base_delay * (2**attempt), 20.0) * (0.5 + random.random() / 2))  # noqa: S311 (jitter)

    async def _rate_limit(self, call: CallContext) -> None:
        """Requests per minute per org and lane, shared by every worker through Key Value."""
        limit = self.rpm.get(call.lane, 60)
        waited = 0.0
        while True:
            now = time.time()
            minute = int(now // 60)
            key = f"ap:t:{call.org_id}:llm-rpm:{call.lane}:{minute}"
            count = await self.kv.incr(key)
            if count == 1:
                await self.kv.expire(key, 120)
            if count <= limit:
                return
            pause = (minute + 1) * 60 - now + 0.05
            if waited + pause > self.max_rate_wait:
                raise LLMUnavailableError(f"org rate limit of {limit} model requests per minute")
            log.info(
                "org model rate limit reached, waiting", extra={"lane": call.lane, "seconds": round(pause, 1)}
            )
            await asyncio.sleep(pause)
            waited += pause

    async def _meter(self, call: CallContext, result: LLMResult) -> None:
        u = result.usage
        async with tenant_tx(self.pool, call.org_id) as conn:
            await conn.execute(
                "insert into app.usage_events (org_id, membership_id, task_id, kind, model, input_tokens, "
                "output_tokens, cached_tokens, cost) values (%s, %s, %s, 'llm', %s, %s, %s, %s, %s)",
                (
                    call.org_id,
                    call.membership_id,
                    call.task_id,
                    result.model,
                    u.billable_input,
                    u.output_tokens,
                    u.cache_read_input_tokens,
                    result.cost,
                ),
            )
            if call.task_id is not None:
                await conn.execute(
                    "update app.tasks set tokens_used = tokens_used + %s, cost = cost + %s where id = %s",
                    (u.counted, result.cost, call.task_id),
                )

    async def aclose(self) -> None:
        await self.http.aclose()


def _error_type(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", {}).get("type", "unknown"))
    except ValueError:
        return "unparseable"


def gateway_from_settings(settings: Any, pool: AsyncConnectionPool, kv: Any) -> LLMGateway | None:
    """None when no model provider key is configured."""
    if settings.anthropic_api_key is None:
        return None
    routes = [Route(settings.anthropic_api_url, settings.anthropic_api_key.get_secret_value())]
    if settings.anthropic_fallback_api_url:
        key = settings.anthropic_fallback_api_key or settings.anthropic_api_key
        routes.append(Route(settings.anthropic_fallback_api_url, key.get_secret_value(), "fallback"))
    return LLMGateway(
        routes,
        pool,
        kv,
        {
            "fast": settings.model_fast,
            "standard": settings.model_standard,
            "advanced": settings.model_advanced,
        },
        {"interactive": settings.llm_org_rpm, "batch": settings.llm_org_batch_rpm},
    )

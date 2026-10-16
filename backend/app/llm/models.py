"""Model tiers, tier resolution and prices.

Admins choose tiers (``fast``, ``standard``, ``advanced``) within one provider family; operators
map tiers to model ids in settings. Prices are USD per million tokens and are estimates to keep
in step with the provider's price list: they drive budgets and usage reports, not billing.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, get_args

Tier = Literal["fast", "standard", "advanced"]
TIERS: tuple[Tier, ...] = get_args(Tier)


@dataclass(frozen=True)
class Price:
    input: Decimal
    output: Decimal
    cache_write: Decimal
    cache_read: Decimal


def _price(input_usd: str, output_usd: str, cache_read_usd: str | None = None) -> Price:
    base = Decimal(input_usd)
    read = Decimal(cache_read_usd) if cache_read_usd is not None else base * Decimal("0.1")
    return Price(base, Decimal(output_usd), base * Decimal("1.25"), read)


# By model id prefix (the longest that matches), so dated snapshots share a price.
PRICES: dict[str, Price] = {
    "claude-haiku": _price("1", "5"),
    "claude-sonnet-5": _price("2", "10"),
    "claude-sonnet": _price("3", "15"),
    "claude-opus-5-5": _price("4", "20", "0.20"),
    "claude-opus": _price("5", "25"),
    "claude-fable-5-1": _price("10", "50", "0.25"),
    "claude-fable": _price("10", "50"),
}
DEFAULT_PRICE = _price("3", "15")
PER_TOKEN = Decimal(1_000_000)

# Models that refuse a forced tool choice (``tool`` or ``any``): the gateway asks with ``auto`` and an
# instruction to call the tool instead; code checks the call either way.
NO_FORCED_TOOL_CHOICE = ("claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5-1")
# Models that think by default or always. Thinking counts against ``max_tokens``, so a step's small
# output limit is raised to leave room for it (only tokens actually generated are paid for).
THINKING = ("claude-sonnet-5", "claude-opus-5", "claude-fable", "claude-mythos")
THINKING_MIN_MAX_TOKENS = 8000


def price_for(model: str) -> Price:
    matches = [prefix for prefix in PRICES if model.startswith(prefix)]
    return PRICES[max(matches, key=len)] if matches else DEFAULT_PRICE


def forces_tools(model: str) -> bool:
    return not model.startswith(NO_FORCED_TOOL_CHOICE)


def thinks(model: str) -> bool:
    return model.startswith(THINKING)


def cost(model: str, input_tokens: int, output_tokens: int, cache_write: int, cache_read: int) -> Decimal:
    p = price_for(model)
    total = (
        input_tokens * p.input
        + output_tokens * p.output
        + cache_write * p.cache_write
        + cache_read * p.cache_read
    )
    return (total / PER_TOKEN).quantize(Decimal("0.000001"))


def resolve_tier(requested: Tier, allowed: list[str], default: str) -> Tier:
    """The requested tier if the org allows it; otherwise the closest allowed tier (the cheaper
    one on a tie); the org default if nothing matches."""
    usable = [t for t in TIERS if t in allowed]
    if requested in usable:
        return requested
    if not usable:
        return next((t for t in TIERS if t == default), "standard")
    want = TIERS.index(requested)
    return min(usable, key=lambda t: (abs(TIERS.index(t) - want), TIERS.index(t)))

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


def _price(input_usd: str, output_usd: str) -> Price:
    base = Decimal(input_usd)
    return Price(base, Decimal(output_usd), base * Decimal("1.25"), base * Decimal("0.1"))


# By model id prefix, so dated snapshots share a price.
PRICES: dict[str, Price] = {
    "claude-haiku": _price("1", "5"),
    "claude-sonnet": _price("3", "15"),
    "claude-opus": _price("5", "25"),
}
DEFAULT_PRICE = _price("3", "15")
PER_TOKEN = Decimal(1_000_000)


def price_for(model: str) -> Price:
    return next((p for prefix, p in PRICES.items() if model.startswith(prefix)), DEFAULT_PRICE)


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

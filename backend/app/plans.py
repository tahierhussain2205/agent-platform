"""Plans (spec 4.8): what an org's admins may set.

The ceilings live in ``app.plans`` (migration 010): the Settings API refuses values above them, new
orgs start on Free with its limits applied, and ``app org plan`` (``app.maintenance_set_plan``)
moves an org to another plan and brings its settings within it.
"""

from dataclasses import dataclass
from typing import Any

import psycopg

TIERS = ("fast", "standard", "advanced")


@dataclass(frozen=True)
class Plan:
    key: str
    title: str
    user_daily_token_limit: int | None  # None: no ceiling, admins may lift the limit
    org_monthly_token_limit: int | None
    max_concurrent_tasks: int
    max_schedules: int
    max_retention_days: int
    model_tiers: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "model_tiers": list(self.model_tiers)}

    def refusal(self, changes: dict[str, Any]) -> str | None:
        """Why these settings are beyond the plan, or None. ``changes`` are the columns being set
        (None for a token limit means no limit)."""
        name = f"The {self.title} plan"
        for column, ceiling, what in (
            ("user_daily_token_limit", self.user_daily_token_limit, "tokens per person per day"),
            ("org_monthly_token_limit", self.org_monthly_token_limit, "tokens per month for the org"),
        ):
            if column in changes and ceiling is not None:
                value = changes[column]
                if value is None:
                    return f"{name} needs a limit on {what} (at most {ceiling:,})."
                if value > ceiling:
                    return f"{name} allows at most {ceiling:,} {what}."
        for column, cap, what in (
            ("max_concurrent_tasks", self.max_concurrent_tasks, "concurrent tasks"),
            ("max_schedules", self.max_schedules, "scheduled reports"),
            ("retention_days", self.max_retention_days, "days of retention"),
        ):
            if column in changes and changes[column] > cap:
                return f"{name} allows at most {cap:,} {what}."
        extra = [t for t in changes.get("allowed_model_tiers") or [] if t not in self.model_tiers]
        if extra:
            return (
                f"{name} does not include the {', '.join(extra)} model tier{'s' if len(extra) > 1 else ''}."
            )
        return None


async def org_plan(conn: psycopg.AsyncConnection[Any]) -> Plan:
    """The plan of the org the connection is bound to."""
    row = await (
        await conn.execute(
            "select p.key, p.title, p.user_daily_token_limit, p.org_monthly_token_limit, "
            "p.max_concurrent_tasks, p.max_schedules, p.max_retention_days, p.model_tiers::text[] "
            "from app.orgs o join app.plans p on p.key = o.plan where o.id = app.current_org_id()"
        )
    ).fetchone()
    if row is None:
        raise LookupError("no org is bound to this connection")
    key, title, user_daily, org_monthly, concurrent, schedules, retention, tiers = row
    return Plan(key, title, user_daily, org_monthly, concurrent, schedules, retention,
                tuple(sorted(tiers, key=TIERS.index)))  # fmt: skip

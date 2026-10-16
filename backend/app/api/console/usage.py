"""Console: usage (spec 4.7 Usage): tokens and cost per day, per person, per playbook and per model,
and today's and this month's use against the org's limits. Days are UTC, as the budgets count them."""

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.api.console.common import fetch_all, fetch_one
from app.auth.console import ConsoleIdentity, require_admin
from app.db.uow import UnitOfWork, get_uow

router = APIRouter(prefix="/api")


@router.get("/usage")
async def usage(
    days: int = Query(default=30, ge=1, le=90),
    identity: ConsoleIdentity = Depends(require_admin),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, Any]:
    conn = uow.connection
    window = {"days": days}
    since = "(now() at time zone 'UTC')::date - (%(days)s - 1)"
    limits = await fetch_one(
        conn,
        """
        select s.user_daily_token_limit, s.org_monthly_token_limit,
               (select coalesce(sum(d.tokens), 0)::bigint from app.usage_daily d
                 where d.day = (now() at time zone 'UTC')::date) as today_tokens,
               (select coalesce(sum(d.cost), 0)::float8 from app.usage_daily d
                 where d.day = (now() at time zone 'UTC')::date) as today_cost,
               (select coalesce(sum(d.tokens), 0)::bigint from app.usage_daily d
                 where d.day >= date_trunc('month', now() at time zone 'UTC')::date) as month_tokens,
               (select coalesce(sum(d.cost), 0)::float8 from app.usage_daily d
                 where d.day >= date_trunc('month', now() at time zone 'UTC')::date) as month_cost
          from app.org_settings s
        """,
    )
    assert limits is not None
    daily = await fetch_all(
        conn,
        f"""
        select g.day::date::text as day, coalesce(sum(d.tokens), 0)::bigint as tokens,
               coalesce(sum(d.cost), 0)::float8 as cost
          from generate_series({since}, (now() at time zone 'UTC')::date, interval '1 day') as g(day)
          left join app.usage_daily d on d.day = g.day::date
         group by g.day order by g.day
        """,  # noqa: S608 - fixed SQL
        window,
    )
    people = await fetch_all(
        conn,
        f"""
        select d.membership_id, u.email, u.name, sum(d.tokens)::bigint as tokens, sum(d.cost)::float8 as cost,
               coalesce(sum(d.tokens) filter (where d.day = (now() at time zone 'UTC')::date), 0)::bigint
                 as today_tokens
          from app.usage_daily d
          left join app.org_memberships m on m.id = d.membership_id
          left join app.users u on u.id = m.user_id
         where d.day >= {since}
         group by d.membership_id, u.email, u.name
         order by sum(d.tokens) desc
         limit 100
        """,  # noqa: S608 - fixed SQL
        window,
    )
    playbooks = await fetch_all(
        conn,
        f"""
        select p.key, p.title, count(distinct e.task_id) as tasks,
               sum(e.input_tokens + e.output_tokens)::bigint as tokens, sum(e.cost)::float8 as cost
          from app.usage_events e
          left join app.tasks t on t.id = e.task_id
          left join app.playbooks p on p.id = t.playbook_id
         where e.occurred_at >= ({since})::timestamp at time zone 'UTC'
         group by p.key, p.title
         order by sum(e.input_tokens + e.output_tokens) desc
        """,  # noqa: S608 - fixed SQL
        window,
    )
    models = await fetch_all(
        conn,
        f"""
        select coalesce(e.model, e.kind) as model, sum(e.input_tokens)::bigint as input_tokens,
               sum(e.output_tokens)::bigint as output_tokens, sum(e.cached_tokens)::bigint as cached_tokens,
               sum(e.sandbox_seconds)::float8 as sandbox_seconds, sum(e.cost)::float8 as cost
          from app.usage_events e
         where e.occurred_at >= ({since})::timestamp at time zone 'UTC'
         group by coalesce(e.model, e.kind)
         order by sum(e.cost) desc
        """,  # noqa: S608 - fixed SQL
        window,
    )
    return {
        "days": days,
        "limits": {
            "user_daily_token_limit": limits["user_daily_token_limit"],
            "org_monthly_token_limit": limits["org_monthly_token_limit"],
        },
        "today": {"tokens": limits["today_tokens"], "cost": limits["today_cost"]},
        "month": {"tokens": limits["month_tokens"], "cost": limits["month_cost"]},
        "daily": daily,
        "people": [
            {**p, "membership_id": str(p["membership_id"]) if p["membership_id"] else None} for p in people
        ],
        "playbooks": [{**p, "key": p["key"] or "other", "title": p["title"] or "Other"} for p in playbooks],
        "models": models,
    }

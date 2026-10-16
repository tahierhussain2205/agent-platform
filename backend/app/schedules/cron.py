"""Cron expressions with a time zone: validation, next runs, report windows and a readable
description. Temporal evaluates the same five field expressions when it fires the schedule."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

MIN_INTERVAL = timedelta(hours=1)
MAX_WINDOW = timedelta(days=31)
DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
FIELDS = re.compile(r"^\s*\S+(\s+\S+){4}\s*$")  # the database's check


class ScheduleError(Exception):
    """A schedule that cannot be saved; the message is safe to show to its owner."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class Cadence:
    cron: str
    timezone: str

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def next_runs(self, after: datetime, count: int = 3) -> list[datetime]:
        it = croniter(self.cron, after.astimezone(self.zone))
        return [it.get_next(datetime) for _ in range(count)]

    def previous_run(self, before: datetime) -> datetime:
        return croniter(self.cron, before.astimezone(self.zone)).get_prev(datetime)

    def window(self, scheduled_at: datetime) -> tuple[datetime, datetime]:
        """What a run reports on: from the previous scheduled time to this one (at most 31 days)."""
        end = scheduled_at.astimezone(UTC)
        start = self.previous_run(end - timedelta(seconds=1)).astimezone(UTC)
        return max(start, end - MAX_WINDOW), end

    def describe(self) -> str:
        return describe(self.cron)


def cadence(cron: str, timezone: str, now: datetime | None = None) -> Cadence:
    """A validated cadence: five fields, a known time zone, and at most one run an hour."""
    cron = " ".join(cron.split())
    if not FIELDS.match(cron) or not croniter.is_valid(cron):
        raise ScheduleError(
            "bad_cron", "That is not a valid cron expression (five fields, like 0 9 * * 1-5)."
        )
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError("bad_timezone", f"Unknown time zone {timezone!r}.") from exc
    result = Cadence(cron, timezone)
    runs = result.next_runs(now or datetime.now(UTC), 48)
    if any(b - a < MIN_INTERVAL for a, b in pairwise(runs)):
        raise ScheduleError("too_frequent", "A report can run at most once an hour.")
    return result


def _time(minute: str, hour: str) -> str | None:
    if minute.isdigit() and hour.isdigit():
        return f"{int(hour):02d}:{int(minute):02d}"
    return None


def _day_names(field: str) -> str | None:
    names = []
    for part in field.split(","):
        if part.isdigit() and int(part) <= 7:
            names.append(DAYS[int(part) % 7])
        else:
            return None
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def describe(cron: str) -> str:
    """Common cadences in words; anything else as the expression itself."""
    minute, hour, dom, month, dow = cron.split()
    at = _time(minute, hour)
    if at and dom == "*" and month == "*":
        if dow == "*":
            return f"Every day at {at}"
        if dow in ("1-5", "MON-FRI", "mon-fri"):
            return f"Every weekday at {at}"
        if (days := _day_names(dow)) is not None:
            return f"Every {days} at {at}"
    if at and dom.isdigit() and month == "*" and dow == "*":
        return f"On day {int(dom)} of every month at {at}"
    if minute.isdigit() and hour == "*" and dom == "*" and month == "*" and dow == "*":
        return f"Every hour at :{int(minute):02d}"
    return f"Cron {cron}"

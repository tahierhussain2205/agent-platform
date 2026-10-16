"""Cadences: validation, next runs in the schedule's time zone (DST included), report windows and
descriptions."""

from datetime import UTC, datetime, timedelta

import pytest

from app.schedules.cron import ScheduleError, cadence, describe

NOW = datetime(2026, 10, 16, 10, 0, tzinfo=UTC)  # a Friday


@pytest.mark.parametrize(
    ("cron", "code"),
    [
        ("0 9 * *", "bad_cron"),
        ("61 9 * * *", "bad_cron"),
        ("0 9 * * * *", "bad_cron"),  # six fields (seconds) are not cron here
        ("*/30 * * * *", "too_frequent"),
        ("0,30 9 * * *", "too_frequent"),
        ("* 9 * * *", "too_frequent"),
    ],
)
def test_invalid_or_too_frequent_cadences_are_refused(cron: str, code: str) -> None:
    with pytest.raises(ScheduleError) as info:
        cadence(cron, "UTC", NOW)
    assert info.value.code == code


def test_unknown_time_zone() -> None:
    with pytest.raises(ScheduleError) as info:
        cadence("0 9 * * *", "Mars/Olympus", NOW)
    assert info.value.code == "bad_timezone"


def test_hourly_is_the_most_frequent_allowed() -> None:
    assert cadence("15 * * * *", "UTC", NOW).cron == "15 * * * *"


def test_next_runs_follow_the_time_zone() -> None:
    weekdays = cadence("0 9 * * 1-5", "Europe/Berlin", NOW)
    runs = weekdays.next_runs(NOW)
    assert [r.isoformat() for r in runs] == [
        "2026-10-19T09:00:00+02:00",  # Monday, skipping the weekend
        "2026-10-20T09:00:00+02:00",
        "2026-10-21T09:00:00+02:00",
    ]


def test_daylight_saving_keeps_the_local_time() -> None:
    daily = cadence("0 9 * * *", "Europe/Berlin", NOW)
    runs = daily.next_runs(datetime(2026, 10, 24, 12, 0, tzinfo=UTC), 2)
    assert [r.isoformat() for r in runs] == ["2026-10-25T09:00:00+01:00", "2026-10-26T09:00:00+01:00"]
    assert runs[0].astimezone(UTC).hour == 8  # 09:00 CET after the change


def test_window_is_from_the_previous_run_to_this_one() -> None:
    daily = cadence("0 9 * * *", "Europe/Berlin", NOW)
    scheduled = datetime(2026, 10, 16, 7, 0, tzinfo=UTC)  # 09:00 Berlin
    start, end = daily.window(scheduled)
    assert (start, end) == (datetime(2026, 10, 15, 7, 0, tzinfo=UTC), scheduled)
    weekdays = cadence("0 9 * * 1-5", "Europe/Berlin", NOW)
    monday = datetime(2026, 10, 19, 7, 0, tzinfo=UTC)
    assert weekdays.window(monday)[0] == datetime(2026, 10, 16, 7, 0, tzinfo=UTC)  # covers the weekend
    monthly = cadence("0 9 1 * *", "UTC", NOW)
    start, end = monthly.window(datetime(2026, 11, 1, 9, 0, tzinfo=UTC))
    assert end - start == timedelta(days=31)  # October has 31 days: exactly the cap


def test_window_is_capped() -> None:
    yearly = cadence("0 9 1 1 *", "UTC", NOW)
    start, end = yearly.window(datetime(2027, 1, 1, 9, 0, tzinfo=UTC))
    assert end - start == timedelta(days=31)


@pytest.mark.parametrize(
    ("cron", "text"),
    [
        ("0 9 * * *", "Every day at 09:00"),
        ("30 8 * * 1-5", "Every weekday at 08:30"),
        ("0 10 * * 1", "Every Monday at 10:00"),
        ("0 10 * * 1,3", "Every Monday and Wednesday at 10:00"),
        ("0 17 * * 1,3,5", "Every Monday, Wednesday and Friday at 17:00"),
        ("0 9 1 * *", "On day 1 of every month at 09:00"),
        ("5 * * * *", "Every hour at :05"),
        ("0 9 * 1 *", "Cron 0 9 * 1 *"),
    ],
)
def test_descriptions(cron: str, text: str) -> None:
    assert describe(cron) == text


def test_whitespace_is_normalised() -> None:
    assert cadence("  0   9 * * 1-5 ", "UTC", NOW).cron == "0 9 * * 1-5"

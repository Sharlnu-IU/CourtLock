"""ISO weeks (BR-18) and weekly series occurrences (BR-16), including clock changes."""

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from courtlock.domain.slots import series_occurrences, validate_series
from courtlock.domain.types import VenueConfig
from courtlock.domain.weeks import parse_iso_week, week_bounds_utc
from courtlock.errors import InvalidSlot, ValidationFailed

NEW_YORK = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def venue(tz: str = "UTC", opens: time = time(6), closes: time = time(22)) -> VenueConfig:
    return VenueConfig(tz, opens, closes, 2000, 1200, "USD", ())


@pytest.mark.parametrize(
    ("text", "monday"),
    [
        ("2026-W48", date(2026, 11, 23)),
        ("2026-W01", date(2025, 12, 29)),  # ISO week 1 can start in the previous year
        ("2026-W53", date(2026, 12, 28)),  # 2026 really has 53 ISO weeks
    ],
)
def test_parse_iso_week(text: str, monday: date) -> None:
    assert parse_iso_week(text) == monday


@pytest.mark.parametrize(
    "text", ["2026-48", "2026-W5", "W48", "2026-W00", "2027-W53", "2026-W54", ""]
)
def test_bad_weeks_are_rejected(text: str) -> None:
    with pytest.raises(ValidationFailed):
        parse_iso_week(text)


def test_a_utc_week_is_168_hours() -> None:
    start, end = week_bounds_utc(date(2026, 11, 23), "UTC")
    assert (end - start).total_seconds() == 168 * 3600


def test_week_bounds_follow_the_venues_midnight() -> None:
    start, _ = week_bounds_utc(date(2026, 11, 23), "Asia/Kolkata")
    assert start == datetime(2026, 11, 22, 18, 30, tzinfo=UTC)  # Monday 00:00 IST


def test_a_spring_forward_week_is_167_hours_and_fall_back_is_169() -> None:
    start, end = week_bounds_utc(date(2027, 3, 8), "America/New_York")  # DST starts Sun 14 Mar
    assert (end - start).total_seconds() == 167 * 3600
    start, end = week_bounds_utc(date(2027, 11, 1), "America/New_York")  # DST ends Sun 7 Nov
    assert (end - start).total_seconds() == 169 * 3600


def test_series_is_one_slot_per_week_in_order() -> None:
    first = datetime(2026, 12, 1, 10, tzinfo=UTC)
    starts = series_occurrences(venue(), first, 4)
    assert [s.day for s in starts] == [1, 8, 15, 22]
    assert {s.hour for s in starts} == {10}


def test_series_keeps_the_local_wall_clock_across_a_clock_change() -> None:
    # Saturday 10:00 New York from 6 Mar 2027; the clocks change on Sunday 14 Mar.
    first = datetime(2027, 3, 6, 10, tzinfo=NEW_YORK)
    starts = series_occurrences(venue("America/New_York", time(0), time(23)), first, 3)
    assert [s.astimezone(NEW_YORK).hour for s in starts] == [10, 10, 10]  # always 10:00 local
    assert [s.hour for s in starts] == [15, 15, 14]  # 6, 13 Mar are EST (15Z); 20 Mar is EDT (14Z)


def test_series_over_a_nonexistent_local_hour_is_rejected() -> None:
    # 02:30 on 14 Mar 2027 does not exist in New York.
    first = datetime(2027, 3, 7, 2, 30, tzinfo=NEW_YORK)
    with pytest.raises(InvalidSlot):
        series_occurrences(venue("America/New_York", time(0), time(23)), first, 2)


def test_only_the_first_slot_must_be_inside_the_horizon() -> None:
    first = datetime(2027, 2, 16, 10, tzinfo=UTC)  # 88 days out: fine
    assert len(validate_series(venue(), first, 12, NOW, 90)) == 12  # week 12 is ~170 days out
    with pytest.raises(InvalidSlot):
        validate_series(venue(), datetime(2027, 3, 1, 10, tzinfo=UTC), 3, NOW, 90)  # 101 days out


def test_every_occurrence_must_be_a_real_slot() -> None:
    with pytest.raises(InvalidSlot):
        validate_series(venue(), datetime(2026, 12, 1, 10, 30, tzinfo=UTC), 3, NOW, 90)

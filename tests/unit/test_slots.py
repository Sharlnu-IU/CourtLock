"""BR-1, BR-3, BR-4: one-hour slots on the hour in the venue's local time."""

from datetime import UTC, date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from courtlock.domain.slots import slots_for, validate_slot
from courtlock.domain.types import VenueConfig
from courtlock.errors import InvalidSlot

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def venue(tz: str = "UTC", opens: time = time(6), closes: time = time(22)) -> VenueConfig:
    return VenueConfig(tz, opens, closes, 2000, 1200, "USD", ())


def test_utc_venue_has_sixteen_hourly_slots() -> None:
    slots = slots_for(venue(), date(2026, 12, 1))
    assert len(slots) == 16
    assert slots[0] == datetime(2026, 12, 1, 6, tzinfo=UTC)
    assert slots[-1] == datetime(2026, 12, 1, 21, tzinfo=UTC)  # 21:00-22:00 is the last slot


def test_half_hour_offset_zone_slots_start_at_half_past_utc() -> None:
    slots = slots_for(venue("Asia/Kolkata"), date(2026, 12, 1))
    assert slots[0] == datetime(2026, 12, 1, 0, 30, tzinfo=UTC)  # 06:00 IST
    assert all(s.minute == 30 for s in slots)


def test_spring_forward_skips_the_hour_that_does_not_exist() -> None:
    # New York, 2026-03-08: clocks jump from 02:00 to 03:00.
    zone = ZoneInfo("America/New_York")
    slots = slots_for(venue("America/New_York", time(0), time(6)), date(2026, 3, 8))
    assert [s.astimezone(zone).hour for s in slots] == [0, 1, 3, 4, 5]


def test_fall_back_uses_first_occurrence_of_the_repeated_hour() -> None:
    # New York, 2026-11-01: 01:00-02:00 happens twice; we offer it once.
    slots = slots_for(venue("America/New_York", time(0), time(4)), date(2026, 11, 1))
    assert len(slots) == 4


@pytest.mark.parametrize(
    "start",
    [
        datetime(2026, 12, 1, 6, tzinfo=UTC),  # first slot
        datetime(2026, 12, 1, 21, tzinfo=UTC),  # last slot
    ],
)
def test_valid_slots_are_accepted(start: datetime) -> None:
    validate_slot(venue(), start, NOW, 90)


@pytest.mark.parametrize(
    ("start", "why"),
    [
        (datetime(2026, 12, 1, 10, 30, tzinfo=UTC), "not on the hour"),
        (datetime(2026, 12, 1, 5, tzinfo=UTC), "before opening"),
        (datetime(2026, 12, 1, 22, tzinfo=UTC), "slot would end after closing"),
        (datetime(2026, 11, 19, 10, tzinfo=UTC), "in the past"),
        (NOW, "starting right now"),
        (datetime(2027, 6, 1, 10, tzinfo=UTC), "beyond the 90 day horizon"),
        (datetime(2026, 12, 1, 10), "no UTC offset"),
    ],
)
def test_invalid_slots_are_rejected(start: datetime, why: str) -> None:
    with pytest.raises(InvalidSlot):
        validate_slot(venue(), start, NOW, 90)


def test_alignment_is_judged_in_venue_local_time() -> None:
    kolkata = venue("Asia/Kolkata")
    validate_slot(kolkata, datetime(2026, 12, 1, 4, 30, tzinfo=UTC), NOW, 90)  # 10:00 IST
    with pytest.raises(InvalidSlot):
        validate_slot(kolkata, datetime(2026, 12, 1, 10, 0, tzinfo=UTC), NOW, 90)  # 15:30 IST


def test_offset_in_request_is_honoured() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    # 10:00+05:30 is 04:30Z: a valid Kolkata slot.
    validate_slot(venue("Asia/Kolkata"), datetime(2026, 12, 1, 10, 0, tzinfo=ist), NOW, 90)

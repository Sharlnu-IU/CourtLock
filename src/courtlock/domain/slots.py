"""Slot rules (BR-1, BR-3, BR-4): one-hour slots on the hour in the venue's local time."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from courtlock.domain.types import VenueConfig
from courtlock.errors import InvalidSlot

SLOT_LENGTH = timedelta(hours=1)


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def slots_for(venue: VenueConfig, local_date: date) -> list[datetime]:
    """UTC start instants of every slot the venue offers on one venue-local date.

    A local hour that does not exist (spring-forward gap) is skipped; an ambiguous hour
    (fall-back) uses its first occurrence.
    """
    zone = ZoneInfo(venue.timezone)
    closes = _minutes(venue.closes_at)
    minute = _minutes(venue.opens_at)
    starts: list[datetime] = []
    while minute + 60 <= closes:
        naive = datetime.combine(local_date, time(minute // 60, minute % 60))
        start_utc = naive.replace(tzinfo=zone).astimezone(UTC)
        if start_utc.astimezone(zone).replace(tzinfo=None) == naive:
            starts.append(start_utc)
        minute += 60
    return starts


def validate_slot(venue: VenueConfig, start_at: datetime, now: datetime, horizon_days: int) -> None:
    """Raise InvalidSlot unless start_at is a real, future, in-horizon slot of this venue."""
    if start_at.tzinfo is None:
        raise InvalidSlot("start_at must include a UTC offset")
    start_utc = start_at.astimezone(UTC)
    if start_utc <= now:
        raise InvalidSlot("start_at is not in the future")
    if start_utc > now + timedelta(days=horizon_days):
        raise InvalidSlot(f"start_at is more than {horizon_days} days ahead")
    local_date = start_utc.astimezone(ZoneInfo(venue.timezone)).date()
    if start_utc not in slots_for(venue, local_date):
        raise InvalidSlot(
            "start_at must be on the hour in the venue's local time and inside opening hours"
        )


def series_occurrences(venue: VenueConfig, first_start_at: datetime, weeks: int) -> list[datetime]:
    """UTC starts of a weekly series: the same venue-local wall-clock time every 7 days (BR-16).

    A game at 10:00 stays at 10:00 across a clock change. If the wall-clock time does not exist
    on one of the days (spring-forward gap) the series cannot be created as asked.
    """
    zone = ZoneInfo(venue.timezone)
    first_local = first_start_at.astimezone(zone).replace(tzinfo=None)
    starts: list[datetime] = []
    for week in range(weeks):
        naive = first_local + timedelta(days=7 * week)
        start_utc = naive.replace(tzinfo=zone).astimezone(UTC)
        if start_utc.astimezone(zone).replace(tzinfo=None) != naive:
            raise InvalidSlot(f"{naive:%Y-%m-%d %H:%M} does not exist in {venue.timezone}")
        starts.append(start_utc)
    return starts


def validate_series(
    venue: VenueConfig, first_start_at: datetime, weeks: int, now: datetime, horizon_days: int
) -> list[datetime]:
    """Every occurrence must be a real future slot. The horizon (BR-4) binds the FIRST one;
    later weeks may extend up to `weeks` weeks beyond it."""
    validate_slot(venue, first_start_at, now, horizon_days)
    starts = series_occurrences(venue, first_start_at, weeks)
    for start in starts[1:]:
        validate_slot(venue, start, now, horizon_days + 7 * weeks)
    return starts

"""ISO weeks, Monday to Sunday, in a venue's local time (BR-18)."""

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from courtlock.errors import ValidationFailed

_WEEK = re.compile(r"^(\d{4})-W(\d{2})$")


def parse_iso_week(value: str) -> date:
    """'2026-W48' -> the Monday of that ISO week."""
    match = _WEEK.match(value)
    if match is None:
        raise ValidationFailed("week must look like 2026-W48")
    try:
        return date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    except ValueError as exc:
        raise ValidationFailed(f"{value} is not a real ISO week") from exc


def week_bounds_utc(monday: date, timezone: str) -> tuple[datetime, datetime]:
    """[start, end) of the venue-local week as UTC instants (a DST week is 167 or 169 h)."""
    zone = ZoneInfo(timezone)

    def local_midnight(day: date) -> datetime:
        return datetime.combine(day, time()).replace(tzinfo=zone).astimezone(UTC)

    return local_midnight(monday), local_midnight(monday + timedelta(days=7))

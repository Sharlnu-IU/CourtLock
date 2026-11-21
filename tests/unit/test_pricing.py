"""BR-9: peak / off-peak pricing, evaluated in the venue's local time."""

from datetime import UTC, datetime, time

import pytest

from courtlock.domain.pricing import price_for
from courtlock.domain.types import PeakWindowConfig, PriceBand, VenueConfig


def venue(tz: str = "UTC") -> VenueConfig:
    weekday_evenings = tuple(PeakWindowConfig(d, time(17), time(21)) for d in range(5))
    weekend_days = tuple(PeakWindowConfig(d, time(8), time(20)) for d in (5, 6))
    return VenueConfig(
        timezone=tz,
        opens_at=time(6),
        closes_at=time(22),
        peak_price_cents=2000,
        offpeak_price_cents=1200,
        currency="USD",
        peak_windows=weekday_evenings + weekend_days,
    )


# 2026-11-23 is a Monday; 2026-11-28 a Saturday.
@pytest.mark.parametrize(
    ("when", "band", "price"),
    [
        (datetime(2026, 11, 23, 16, tzinfo=UTC), PriceBand.OFFPEAK, 1200),  # just before window
        (datetime(2026, 11, 23, 17, tzinfo=UTC), PriceBand.PEAK, 2000),  # window start inclusive
        (datetime(2026, 11, 23, 20, tzinfo=UTC), PriceBand.PEAK, 2000),  # last peak hour
        (datetime(2026, 11, 23, 21, tzinfo=UTC), PriceBand.OFFPEAK, 1200),  # window end exclusive
        (datetime(2026, 11, 28, 7, tzinfo=UTC), PriceBand.OFFPEAK, 1200),  # before weekend window
        (datetime(2026, 11, 28, 8, tzinfo=UTC), PriceBand.PEAK, 2000),  # weekend all day
        (datetime(2026, 11, 28, 19, tzinfo=UTC), PriceBand.PEAK, 2000),
    ],
)
def test_utc_venue(when: datetime, band: PriceBand, price: int) -> None:
    assert price_for(venue(), when) == (band, price)


def test_band_is_decided_in_venue_local_time() -> None:
    kolkata = venue("Asia/Kolkata")
    # 11:30 UTC on Monday is 17:00 IST -> peak; 10:30 UTC is 16:00 IST -> off-peak.
    assert price_for(kolkata, datetime(2026, 11, 23, 11, 30, tzinfo=UTC)) == (
        PriceBand.PEAK,
        2000,
    )
    assert price_for(kolkata, datetime(2026, 11, 23, 10, 30, tzinfo=UTC)) == (
        PriceBand.OFFPEAK,
        1200,
    )


def test_local_weekday_can_differ_from_utc_weekday() -> None:
    # Sunday 21:00 UTC is Monday 02:30 IST: a Monday morning, which is off-peak.
    sunday_night_utc = datetime(2026, 11, 22, 21, 0, tzinfo=UTC)
    assert price_for(venue("Asia/Kolkata"), sunday_night_utc)[0] is PriceBand.OFFPEAK


def test_venue_without_windows_is_always_offpeak() -> None:
    bare = VenueConfig("UTC", time(6), time(22), 2000, 1200, "USD", ())
    assert price_for(bare, datetime(2026, 11, 23, 18, tzinfo=UTC)) == (PriceBand.OFFPEAK, 1200)

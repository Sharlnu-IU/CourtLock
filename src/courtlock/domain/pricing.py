"""Peak / off-peak pricing (BR-9). Evaluated in the venue's local time."""

from datetime import datetime
from zoneinfo import ZoneInfo

from courtlock.domain.types import PriceBand, VenueConfig


def price_for(venue: VenueConfig, start_at: datetime) -> tuple[PriceBand, int]:
    local = start_at.astimezone(ZoneInfo(venue.timezone))
    local_time = local.time().replace(second=0, microsecond=0)
    for window in venue.peak_windows:
        if window.weekday == local.weekday() and window.start <= local_time < window.end:
            return PriceBand.PEAK, venue.peak_price_cents
    return PriceBand.OFFPEAK, venue.offpeak_price_cents

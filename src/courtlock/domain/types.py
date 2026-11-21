"""Plain value types shared by the domain functions."""

import enum
from dataclasses import dataclass
from datetime import time


class PriceBand(enum.Enum):
    PEAK = "PEAK"
    OFFPEAK = "OFFPEAK"


@dataclass(frozen=True)
class PeakWindowConfig:
    weekday: int  # 0 = Monday, venue-local
    start: time
    end: time


@dataclass(frozen=True)
class VenueConfig:
    timezone: str
    opens_at: time
    closes_at: time
    peak_price_cents: int
    offpeak_price_cents: int
    currency: str
    peak_windows: tuple[PeakWindowConfig, ...]

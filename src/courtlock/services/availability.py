"""GET /courts/{id}/availability (FR-4): every slot in a venue-local date range, with status."""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from courtlock.db.models import BookingStatus, live_bookings
from courtlock.domain.pricing import price_for
from courtlock.domain.slots import SLOT_LENGTH, slots_for
from courtlock.domain.types import PriceBand
from courtlock.errors import ValidationFailed
from courtlock.services.common import db_now, load_court, venue_config

MAX_SPAN_DAYS = 14


@dataclass(frozen=True)
class SlotView:
    start_at: datetime
    end_at: datetime
    status: str  # AVAILABLE | HELD | BOOKED
    band: PriceBand
    price_cents: int
    currency: str


@dataclass(frozen=True)
class Availability:
    court_id: uuid.UUID
    timezone: str
    slots: list[SlotView]


def get_availability(
    session: Session,
    court_id: uuid.UUID,
    from_date: date | None,
    to_date: date | None,
) -> Availability:
    with session.begin():
        now = db_now(session)
        _, venue = load_court(session, court_id)
        config = venue_config(venue)
        zone = ZoneInfo(config.timezone)

        first = from_date or now.astimezone(zone).date()
        last = to_date or first + timedelta(days=1)  # exclusive
        if last <= first:
            raise ValidationFailed("'to' must be after 'from'")
        if (last - first).days > MAX_SPAN_DAYS:
            raise ValidationFailed(f"at most {MAX_SPAN_DAYS} days can be requested at once")

        starts = [
            start
            for offset in range((last - first).days)
            for start in slots_for(config, first + timedelta(days=offset))
            if start > now  # slots that have started are omitted
        ]
        if not starts:
            return Availability(court_id, config.timezone, [])

        # "Live" comes from the view, so an expired but unswept hold shows as AVAILABLE (BR-6).
        taken = {
            row.start_at.astimezone(UTC): row.status
            for row in session.execute(
                select(live_bookings.c.start_at, live_bookings.c.status).where(
                    live_bookings.c.court_id == court_id,
                    live_bookings.c.start_at >= starts[0],
                    live_bookings.c.start_at <= starts[-1],
                )
            )
        }

        slots: list[SlotView] = []
        for start in starts:
            band, price_cents = price_for(config, start)
            held = taken.get(start)
            status = (
                "AVAILABLE"
                if held is None
                else ("BOOKED" if held == BookingStatus.CONFIRMED else "HELD")
            )
            slots.append(
                SlotView(start, start + SLOT_LENGTH, status, band, price_cents, config.currency)
            )
        return Availability(court_id, config.timezone, slots)

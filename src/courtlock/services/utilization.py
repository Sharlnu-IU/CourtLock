"""GET /staff/utilization (FR-6, BR-18): confirmed booked hours / bookable hours, per week."""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from courtlock.db.models import Booking, BookingStatus, Court, Venue
from courtlock.domain.slots import slots_for
from courtlock.domain.types import PriceBand
from courtlock.domain.weeks import week_bounds_utc
from courtlock.errors import VenueNotFound
from courtlock.services.common import venue_config


@dataclass(frozen=True)
class CourtUtilization:
    court_id: uuid.UUID
    name: str
    booked_hours: int
    bookable_hours: int
    utilization_pct: float
    peak_hours: int
    offpeak_hours: int


@dataclass(frozen=True)
class VenueUtilization:
    venue_id: uuid.UUID
    name: str
    timezone: str
    booked_hours: int
    bookable_hours: int
    utilization_pct: float
    courts: list[CourtUtilization]


def _pct(booked: int, bookable: int) -> float:
    return round(100 * booked / bookable, 1) if bookable else 0.0


def get_utilization(
    session: Session, monday: date, venue_id: uuid.UUID | None
) -> list[VenueUtilization]:
    with session.begin():
        query = select(Venue).order_by(Venue.name)
        if venue_id is not None:
            query = query.where(Venue.id == venue_id)
        venues = session.execute(query).scalars().all()
        if venue_id is not None and not venues:
            raise VenueNotFound("unknown venue")
        return [_venue_utilization(session, venue, monday) for venue in venues]


def _venue_utilization(session: Session, venue: Venue, monday: date) -> VenueUtilization:
    config = venue_config(venue)
    # Bookable hours come from the real slots on each local day, so a clock-change week is right.
    bookable_per_court = sum(len(slots_for(config, monday + timedelta(days=d))) for d in range(7))
    start, end = week_bounds_utc(monday, venue.timezone)

    courts = (
        session.execute(
            select(Court)
            .where(Court.venue_id == venue.id, Court.is_active.is_(True))
            .order_by(Court.name)
        )
        .scalars()
        .all()
    )
    counts: dict[uuid.UUID, dict[PriceBand, int]] = defaultdict(dict)
    for court_id, band, hours in session.execute(
        select(Booking.court_id, Booking.band, func.count())
        .where(
            Booking.court_id.in_([c.id for c in courts]),
            Booking.status == BookingStatus.CONFIRMED,  # holds and cancelled bookings never count
            Booking.start_at >= start,
            Booking.start_at < end,
        )
        .group_by(Booking.court_id, Booking.band)
    ):
        counts[court_id][band] = hours

    rows = []
    for court in courts:
        peak = counts[court.id].get(PriceBand.PEAK, 0)
        offpeak = counts[court.id].get(PriceBand.OFFPEAK, 0)
        rows.append(
            CourtUtilization(
                court_id=court.id,
                name=court.name,
                booked_hours=peak + offpeak,
                bookable_hours=bookable_per_court,
                utilization_pct=_pct(peak + offpeak, bookable_per_court),
                peak_hours=peak,
                offpeak_hours=offpeak,
            )
        )
    booked = sum(r.booked_hours for r in rows)
    bookable = sum(r.bookable_hours for r in rows)
    return VenueUtilization(
        venue_id=venue.id,
        name=venue.name,
        timezone=venue.timezone,
        booked_hours=booked,
        bookable_hours=bookable,
        utilization_pct=_pct(booked, bookable),
        courts=rows,
    )

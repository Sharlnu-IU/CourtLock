"""Helpers shared by the use-cases."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from courtlock.db.models import Court, Member, Venue
from courtlock.domain.types import PeakWindowConfig, VenueConfig
from courtlock.errors import CourtNotFound, MemberNotFound

EXCLUSION_VIOLATION = "23P01"


def db_now(session: Session) -> datetime:
    """The one clock (BR-22): Postgres' wall clock at the moment of the call."""
    value: datetime = session.execute(select(func.clock_timestamp())).scalar_one()
    return value.astimezone(UTC)


def lock_member(session: Session, member_id: uuid.UUID) -> None:
    """Row-lock the member: serialises that member's cap checks and replays."""
    found = session.execute(
        select(Member.id).where(Member.id == member_id).with_for_update()
    ).scalar_one_or_none()
    if found is None:
        raise MemberNotFound("unknown member")


def load_court(session: Session, court_id: uuid.UUID) -> tuple[Court, Venue]:
    row = session.execute(
        select(Court, Venue).join(Venue, Court.venue_id == Venue.id).where(Court.id == court_id)
    ).one_or_none()
    if row is None or not row[0].is_active:
        raise CourtNotFound("unknown court")
    return row[0], row[1]


def venue_config(venue: Venue) -> VenueConfig:
    return VenueConfig(
        timezone=venue.timezone,
        opens_at=venue.opens_at,
        closes_at=venue.closes_at,
        peak_price_cents=venue.peak_price_cents,
        offpeak_price_cents=venue.offpeak_price_cents,
        currency=venue.currency,
        peak_windows=tuple(
            PeakWindowConfig(weekday=w.weekday, start=w.start_time, end=w.end_time)
            for w in venue.peak_windows
        ),
    )


def is_exclusion_violation(exc: IntegrityError) -> bool:
    return getattr(exc.orig, "sqlstate", None) == EXCLUSION_VIOLATION

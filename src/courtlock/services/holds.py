"""POST /holds: reserve a slot briefly (BR-5)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import distinct, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from courtlock.config import Settings
from courtlock.db.models import Booking, BookingStatus, live_bookings
from courtlock.db.retry import run_with_retry
from courtlock.domain.pricing import price_for
from courtlock.domain.slots import SLOT_LENGTH, validate_slot
from courtlock.errors import BookingLimitReached, SlotUnavailable
from courtlock.services.common import (
    db_now,
    is_exclusion_violation,
    load_court,
    lock_member,
    venue_config,
)


@dataclass(frozen=True)
class HoldResult:
    booking: Booking
    created: bool  # False: the member's own live hold for this slot was returned (retry)


def create_hold(
    session: Session,
    settings: Settings,
    member_id: uuid.UUID,
    court_id: uuid.UUID,
    start_at: datetime,
) -> HoldResult:
    return run_with_retry(lambda: _create_hold(session, settings, member_id, court_id, start_at))


def _create_hold(
    session: Session,
    settings: Settings,
    member_id: uuid.UUID,
    court_id: uuid.UUID,
    start_at: datetime,
) -> HoldResult:
    start_utc = start_at.astimezone(UTC)
    with session.begin():
        # First statement: serialise this member's holds so the cap cannot be raced.
        lock_member(session, member_id)
        now = db_now(session)

        _, venue = load_court(session, court_id)
        config = venue_config(venue)
        validate_slot(config, start_utc, now, settings.booking_horizon_days)

        # Retry of a lost response: hand back the member's own live hold for this exact slot.
        existing_id = session.execute(
            select(live_bookings.c.id).where(
                live_bookings.c.member_id == member_id,
                live_bookings.c.court_id == court_id,
                live_bookings.c.start_at == start_utc,
                live_bookings.c.status == BookingStatus.HELD,
            )
        ).scalar_one_or_none()
        if existing_id is not None:
            existing = session.get(Booking, existing_id)
            if existing is not None:
                return HoldResult(existing, created=False)

        # BR-10: a standalone booking/hold counts once; a whole recurring series counts once.
        active = session.execute(
            select(
                func.count(distinct(func.coalesce(live_bookings.c.series_id, live_bookings.c.id)))
            )
            .where(live_bookings.c.member_id == member_id)
            .where(live_bookings.c.start_at > now)
        ).scalar_one()
        if active >= settings.max_active_bookings:
            raise BookingLimitReached(
                f"members may hold at most {settings.max_active_bookings} active future bookings"
            )

        band, price_cents = price_for(config, start_utc)
        booking = Booking(
            id=uuid.uuid4(),
            member_id=member_id,
            court_id=court_id,
            series_id=None,
            start_at=start_utc,
            end_at=start_utc + SLOT_LENGTH,
            status=BookingStatus.HELD,
            band=band,
            price_cents=price_cents,
            currency=venue.currency,
            hold_expires_at=min(now + timedelta(seconds=settings.hold_ttl_seconds), start_utc),
        )
        session.add(booking)
        try:
            # The BEFORE INSERT trigger evicts expired holds; the exclusion constraint decides.
            session.flush()
        except IntegrityError as exc:
            if is_exclusion_violation(exc):
                raise SlotUnavailable("that slot is held or booked") from exc
            raise
        return HoldResult(booking, created=True)

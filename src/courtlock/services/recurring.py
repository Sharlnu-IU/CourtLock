"""POST /recurring-bookings: a weekly series, all occurrences or none (BR-16, BR-17).

One transaction. Occurrences are inserted as CONFIRMED rows in ascending time order, so two
racing series always take row locks in the same order (no deadlocks from ordering), and the
exclusion constraint settles any race. The BEFORE INSERT trigger evicts expired holds for each
row, so an expired but unswept hold never makes a free slot look taken (BR-6).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import distinct, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from courtlock.config import Settings
from courtlock.db.models import Booking, BookingStatus, LedgerEntry, RecurringSeries, live_bookings
from courtlock.db.retry import run_with_retry
from courtlock.domain.pricing import price_for
from courtlock.domain.slots import SLOT_LENGTH, validate_series
from courtlock.errors import BookingLimitReached, SeriesConflict
from courtlock.services.common import (
    db_now,
    is_exclusion_violation,
    load_court,
    lock_member,
    venue_config,
)


@dataclass(frozen=True)
class SeriesResult:
    series_id: uuid.UUID
    court_id: uuid.UUID
    member_id: uuid.UUID
    weeks: int
    currency: str
    bookings: list[Booking]

    @property
    def total_price_cents(self) -> int:
        return sum(b.price_cents for b in self.bookings)


def create_series(
    session: Session,
    settings: Settings,
    member_id: uuid.UUID,
    court_id: uuid.UUID,
    first_start_at: datetime,
    weeks: int,
) -> SeriesResult:
    return run_with_retry(
        lambda: _create_series(session, settings, member_id, court_id, first_start_at, weeks)
    )


def _create_series(
    session: Session,
    settings: Settings,
    member_id: uuid.UUID,
    court_id: uuid.UUID,
    first_start_at: datetime,
    weeks: int,
) -> SeriesResult:
    with session.begin():
        lock_member(session, member_id)  # serialises this member's cap check
        now = db_now(session)

        _, venue = load_court(session, court_id)
        config = venue_config(venue)
        starts = validate_series(
            config, first_start_at.astimezone(UTC), weeks, now, settings.booking_horizon_days
        )

        # BR-10: the whole series counts as ONE booking toward the cap.
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

        # Nicer error: name every taken week. `live_bookings` ignores expired holds, so a stale
        # hold is not reported as a conflict. The constraint below is still the authority.
        taken = (
            session.execute(
                select(live_bookings.c.start_at)
                .where(live_bookings.c.court_id == court_id)
                .where(live_bookings.c.start_at.in_(starts))
                .order_by(live_bookings.c.start_at)
            )
            .scalars()
            .all()
        )
        if taken:
            raise SeriesConflict(
                f"{len(taken)} of {weeks} weeks are not available; nothing was booked",
                conflicts=[{"start_at": _iso(t)} for t in taken],
            )

        series = RecurringSeries(
            id=uuid.uuid4(), member_id=member_id, court_id=court_id, weeks=weeks
        )
        session.add(series)
        session.flush()

        bookings: list[Booking] = []
        for start in starts:  # ascending: the same lock order for every racing series
            band, price_cents = price_for(config, start)
            bookings.append(
                Booking(
                    id=uuid.uuid4(),
                    member_id=member_id,
                    court_id=court_id,
                    series_id=series.id,
                    start_at=start,
                    end_at=start + SLOT_LENGTH,
                    status=BookingStatus.CONFIRMED,
                    band=band,
                    price_cents=price_cents,
                    currency=venue.currency,
                    confirmed_at=now,
                )
            )
        session.add_all(bookings)
        try:
            session.flush()
        except IntegrityError as exc:
            if is_exclusion_violation(exc):  # lost a race for one of the weeks
                raise SeriesConflict("a week was taken while booking; nothing was booked") from exc
            raise

        session.add_all(
            LedgerEntry(
                id=uuid.uuid4(),
                booking_id=b.id,
                member_id=member_id,
                kind="CHARGE",
                amount_cents=b.price_cents,
                currency=b.currency,
            )
            for b in bookings
            if b.price_cents > 0
        )
        session.flush()
        return SeriesResult(series.id, court_id, member_id, weeks, venue.currency, bookings)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

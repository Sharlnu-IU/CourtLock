"""Confirm a hold (BR-7, BR-15) and cancel a booking (BR-11..14)."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from courtlock.db.models import Booking, BookingStatus, LedgerEntry
from courtlock.db.retry import run_with_retry
from courtlock.domain.refund import refund_amount, refund_percent
from courtlock.errors import (
    BookingNotFound,
    BookingStarted,
    HoldExpired,
    HoldNotFound,
    InvalidState,
)
from courtlock.services.common import db_now


@dataclass(frozen=True)
class ConfirmResult:
    booking: Booking
    replayed: bool


def confirm_hold(session: Session, member_id: uuid.UUID, hold_id: uuid.UUID) -> ConfirmResult:
    return run_with_retry(lambda: _confirm_hold(session, member_id, hold_id))


def _confirm_hold(session: Session, member_id: uuid.UUID, hold_id: uuid.UUID) -> ConfirmResult:
    with session.begin():
        # One conditional UPDATE decides: it blocks on the row lock if another confirm is in
        # flight, then re-checks its WHERE clause against the committed row (READ COMMITTED).
        confirmed = session.execute(
            update(Booking)
            .where(
                Booking.id == hold_id,
                Booking.member_id == member_id,
                Booking.status == BookingStatus.HELD,
                Booking.hold_expires_at > func.clock_timestamp(),
            )
            .values(status=BookingStatus.CONFIRMED, confirmed_at=func.clock_timestamp())
            .returning(Booking)
            .execution_options(synchronize_session=False)
        ).scalar_one_or_none()
        if confirmed is not None:
            if confirmed.price_cents > 0:
                session.add(
                    LedgerEntry(
                        id=uuid.uuid4(),
                        booking_id=confirmed.id,
                        member_id=member_id,
                        kind="CHARGE",
                        amount_cents=confirmed.price_cents,
                        currency=confirmed.currency,
                    )
                )
                session.flush()
            return ConfirmResult(confirmed, replayed=False)

        # Nothing updated: work out why. Nothing was written, so there is nothing to undo.
        current = session.execute(
            select(Booking).where(Booking.id == hold_id, Booking.member_id == member_id)
        ).scalar_one_or_none()
        if current is None:
            raise HoldNotFound("no such hold")
        if current.status == BookingStatus.CONFIRMED:
            return ConfirmResult(current, replayed=True)
        if current.status in (BookingStatus.HELD, BookingStatus.EXPIRED):
            raise HoldExpired("the hold has expired; hold the slot again")
        raise InvalidState(f"booking is {current.status.value}")


def cancel_booking(session: Session, member_id: uuid.UUID, booking_id: uuid.UUID) -> Booking:
    return run_with_retry(lambda: _cancel_booking(session, member_id, booking_id))


def _cancel_booking(session: Session, member_id: uuid.UUID, booking_id: uuid.UUID) -> Booking:
    with session.begin():
        booking = session.execute(
            select(Booking)
            .where(Booking.id == booking_id, Booking.member_id == member_id)
            .with_for_update()
        ).scalar_one_or_none()
        if booking is None:
            raise BookingNotFound("no such booking")
        now = db_now(session)  # read after the lock, when we actually act

        if booking.status == BookingStatus.CANCELLED:
            return booking  # replay: the stored result, never a second refund

        if booking.status == BookingStatus.HELD:
            if booking.hold_expires_at is None or booking.hold_expires_at <= now:
                raise HoldExpired("the hold has expired")
            _mark_cancelled(booking, now, percent=0)  # releasing a hold moves no money
            return booking

        if booking.status != BookingStatus.CONFIRMED:
            raise HoldExpired("the hold has expired")
        if booking.start_at <= now:
            raise BookingStarted("a booking that has started cannot be cancelled")

        percent = refund_percent(booking.start_at, now)
        _mark_cancelled(booking, now, percent)
        refund = refund_amount(booking.price_cents, percent)
        if refund > 0:
            session.add(
                LedgerEntry(
                    id=uuid.uuid4(),
                    booking_id=booking.id,
                    member_id=member_id,
                    kind="REFUND",
                    amount_cents=refund,
                    currency=booking.currency,
                )
            )
        session.flush()
        return booking


def _mark_cancelled(booking: Booking, now: datetime, percent: int) -> None:
    booking.status = BookingStatus.CANCELLED
    booking.cancelled_at = now
    booking.refund_percent = percent
    booking.refund_cents = refund_amount(booking.price_cents, percent)

"""Request and response bodies (the contract documented in the README).

All response datetimes are converted to UTC here, so they serialise with a trailing 'Z'.
"""

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel

from courtlock.db.models import Booking
from courtlock.domain.types import PriceBand
from courtlock.errors import InvalidState
from courtlock.services.availability import Availability


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


class HoldCreate(BaseModel):
    court_id: uuid.UUID
    start_at: AwareDatetime


class HoldResponse(BaseModel):
    id: uuid.UUID
    court_id: uuid.UUID
    member_id: uuid.UUID
    start_at: datetime
    end_at: datetime
    status: Literal["HELD"]
    price_band: PriceBand
    price_cents: int
    currency: str
    expires_at: datetime

    @classmethod
    def from_booking(cls, booking: Booking) -> "HoldResponse":
        if booking.hold_expires_at is None:
            raise InvalidState("booking is not a hold")
        return cls(
            id=booking.id,
            court_id=booking.court_id,
            member_id=booking.member_id,
            start_at=_utc(booking.start_at),
            end_at=_utc(booking.end_at),
            status="HELD",
            price_band=booking.band,
            price_cents=booking.price_cents,
            currency=booking.currency,
            expires_at=_utc(booking.hold_expires_at),
        )


class BookingResponse(BaseModel):
    id: uuid.UUID
    court_id: uuid.UUID
    member_id: uuid.UUID
    status: Literal["CONFIRMED"]
    confirmed_at: datetime
    start_at: datetime
    end_at: datetime
    price_band: PriceBand
    price_cents: int
    currency: str

    @classmethod
    def from_booking(cls, booking: Booking) -> "BookingResponse":
        if booking.confirmed_at is None:
            raise InvalidState("booking is not confirmed")
        return cls(
            id=booking.id,
            court_id=booking.court_id,
            member_id=booking.member_id,
            status="CONFIRMED",
            confirmed_at=_utc(booking.confirmed_at),
            start_at=_utc(booking.start_at),
            end_at=_utc(booking.end_at),
            price_band=booking.band,
            price_cents=booking.price_cents,
            currency=booking.currency,
        )


class CancelResponse(BaseModel):
    id: uuid.UUID
    status: Literal["CANCELLED"]
    cancelled_at: datetime
    price_cents: int
    refund_percent: int
    refund_cents: int
    currency: str

    @classmethod
    def from_booking(cls, booking: Booking) -> "CancelResponse":
        if (
            booking.cancelled_at is None
            or booking.refund_percent is None
            or booking.refund_cents is None
        ):
            raise InvalidState("booking is not cancelled")
        return cls(
            id=booking.id,
            status="CANCELLED",
            cancelled_at=_utc(booking.cancelled_at),
            price_cents=booking.price_cents,
            refund_percent=booking.refund_percent,
            refund_cents=booking.refund_cents,
            currency=booking.currency,
        )


class SlotResponse(BaseModel):
    start_at: datetime
    end_at: datetime
    status: Literal["AVAILABLE", "HELD", "BOOKED"]
    price_band: PriceBand
    price_cents: int
    currency: str


class AvailabilityResponse(BaseModel):
    court_id: uuid.UUID
    timezone: str
    slots: list[SlotResponse]

    @classmethod
    def from_availability(cls, availability: Availability) -> "AvailabilityResponse":
        slots = [
            SlotResponse(
                start_at=_utc(slot.start_at),
                end_at=_utc(slot.end_at),
                status=_status(slot.status),
                price_band=slot.band,
                price_cents=slot.price_cents,
                currency=slot.currency,
            )
            for slot in availability.slots
        ]
        return cls(court_id=availability.court_id, timezone=availability.timezone, slots=slots)


def _status(value: str) -> Literal["AVAILABLE", "HELD", "BOOKED"]:
    if value == "HELD":
        return "HELD"
    if value == "BOOKED":
        return "BOOKED"
    return "AVAILABLE"

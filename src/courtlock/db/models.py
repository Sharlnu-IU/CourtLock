"""ORM mapping. The schema itself (constraints, trigger, view) is owned by the Alembic migration."""

import enum
import uuid
from datetime import datetime, time

from sqlalchemy import (
    Boolean,
    Column,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Table,
    Text,
    Time,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import TSTZRANGE, Range
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from courtlock.domain.types import PriceBand


class BookingStatus(enum.Enum):
    HELD = "HELD"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


_status_type = Enum(BookingStatus, name="booking_status", create_type=False)
_band_type = Enum(PriceBand, name="price_band", create_type=False)


class Base(DeclarativeBase):
    pass


class PeakWindow(Base):
    __tablename__ = "peak_windows"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    venue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("venues.id"))
    weekday: Mapped[int] = mapped_column(SmallInteger)
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)


class Venue(Base):
    __tablename__ = "venues"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    timezone: Mapped[str] = mapped_column(Text)
    opens_at: Mapped[time] = mapped_column(Time)
    closes_at: Mapped[time] = mapped_column(Time)
    peak_price_cents: Mapped[int] = mapped_column(Integer)
    offpeak_price_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    peak_windows: Mapped[list[PeakWindow]] = relationship()


class Court(Base):
    __tablename__ = "courts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    venue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("venues.id"))
    name: Mapped[str] = mapped_column(Text)
    sport: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Member(Base):
    __tablename__ = "members"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text)


class Booking(Base):
    """A hold is a booking in status HELD; the hold id is the booking id."""

    __tablename__ = "bookings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    member_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("members.id"))
    court_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("courts.id"))
    series_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    during: Mapped[Range[datetime] | None] = mapped_column(
        TSTZRANGE, Computed("tstzrange(start_at, end_at, '[)')", persisted=True)
    )
    status: Mapped[BookingStatus] = mapped_column(_status_type)
    band: Mapped[PriceBand] = mapped_column(_band_type)
    price_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refund_percent: Mapped[int | None] = mapped_column(SmallInteger)
    refund_cents: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()")
    )


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    booking_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bookings.id"))
    member_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("members.id"))
    kind: Mapped[str] = mapped_column(Text)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()")
    )


# BR-23: the single definition of "live" (CONFIRMED, or HELD and not yet expired).
# Every read that asks "what blocks a slot / what counts toward the cap" goes through it.
live_bookings = Table(
    "live_bookings",
    Base.metadata,
    Column("id", Uuid),
    Column("member_id", Uuid),
    Column("court_id", Uuid),
    Column("series_id", Uuid),
    Column("start_at", DateTime(timezone=True)),
    Column("end_at", DateTime(timezone=True)),
    Column("status", _status_type),
)

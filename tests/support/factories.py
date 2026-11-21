"""Direct-SQL test data. Bypasses the API on purpose, so tests can build any state."""

import uuid
from collections.abc import Sequence
from datetime import datetime, time, timedelta

from sqlalchemy import Engine, text

TABLES = (
    "venues",
    "peak_windows",
    "courts",
    "members",
    "recurring_series",
    "bookings",
    "ledger_entries",
)

WEEKDAY_EVENINGS = (
    (0, time(17), time(21)),
    (1, time(17), time(21)),
    (2, time(17), time(21)),
    (3, time(17), time(21)),
    (4, time(17), time(21)),
)


def make_venue(
    engine: Engine,
    *,
    tz: str = "UTC",
    opens: time = time(6),
    closes: time = time(22),
    peak: int = 2000,
    offpeak: int = 1200,
    currency: str = "USD",
    windows: Sequence[tuple[int, time, time]] = WEEKDAY_EVENINGS,
) -> uuid.UUID:
    venue_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO venues (id, name, timezone, opens_at, closes_at, peak_price_cents,"
                " offpeak_price_cents, currency) VALUES (:id, :name, :tz, :o, :c, :p, :f, :cur)"
            ),
            {
                "id": venue_id,
                "name": f"Venue {venue_id.hex[:6]}",
                "tz": tz,
                "o": opens,
                "c": closes,
                "p": peak,
                "f": offpeak,
                "cur": currency,
            },
        )
        for weekday, start, end in windows:
            conn.execute(
                text(
                    "INSERT INTO peak_windows (id, venue_id, weekday, start_time, end_time)"
                    " VALUES (:id, :v, :w, :s, :e)"
                ),
                {"id": uuid.uuid4(), "v": venue_id, "w": weekday, "s": start, "e": end},
            )
    return venue_id


def make_court(engine: Engine, venue_id: uuid.UUID, name: str | None = None) -> uuid.UUID:
    court_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO courts (id, venue_id, name, sport) VALUES (:id, :v, :n, 'tennis')"),
            {"id": court_id, "v": venue_id, "n": name or f"Court {court_id.hex[:6]}"},
        )
    return court_id


def make_member(engine: Engine) -> uuid.UUID:
    member_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO members (id, name, email) VALUES (:id, :n, :e)"),
            {"id": member_id, "n": "Test Member", "e": f"{member_id.hex}@example.com"},
        )
    return member_id


def make_members(engine: Engine, count: int) -> list[uuid.UUID]:
    return [make_member(engine) for _ in range(count)]


def make_booking(
    engine: Engine,
    *,
    member: uuid.UUID,
    court: uuid.UUID,
    start_at: datetime,
    status: str = "CONFIRMED",
    price: int = 1200,
    hold_expires_at: datetime | None = None,
    charged: bool = True,
) -> uuid.UUID:
    """Insert a booking row directly. A CONFIRMED booking also gets its CHARGE ledger row."""
    booking_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO bookings (id, member_id, court_id, start_at, end_at, status, band,"
                " price_cents, currency, hold_expires_at, confirmed_at)"
                " VALUES (:id, :m, :c, :s, :e, CAST(:st AS booking_status), 'OFFPEAK', :p, 'USD',"
                " :h, CASE WHEN :st = 'CONFIRMED' THEN clock_timestamp() END)"
            ),
            {
                "id": booking_id,
                "m": member,
                "c": court,
                "s": start_at,
                "e": start_at + timedelta(hours=1),
                "st": status,
                "p": price,
                "h": hold_expires_at,
            },
        )
        if status == "CONFIRMED" and charged and price > 0:
            conn.execute(
                text(
                    "INSERT INTO ledger_entries (id, booking_id, member_id, kind, amount_cents,"
                    " currency) VALUES (:id, :b, :m, 'CHARGE', :a, 'USD')"
                ),
                {"id": uuid.uuid4(), "b": booking_id, "m": member, "a": price},
            )
    return booking_id


def backdate_hold(engine: Engine, booking_id: uuid.UUID, seconds: int = 1) -> None:
    """Make a hold already expired (we cannot fake the DB clock, so we move the row)."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE bookings"
                " SET hold_expires_at = clock_timestamp() - make_interval(secs => :s)"
                " WHERE id = :id"
            ),
            {"id": booking_id, "s": seconds},
        )


def booking_status(engine: Engine, booking_id: uuid.UUID) -> str:
    with engine.connect() as conn:
        value: str = conn.execute(
            text("SELECT status::text FROM bookings WHERE id = :id"), {"id": booking_id}
        ).scalar_one()
    return value


def count_rows(engine: Engine, sql: str, **params: object) -> int:
    with engine.connect() as conn:
        value: int = conn.execute(text(sql), params).scalar_one()
    return value


def ledger_count(engine: Engine, booking_id: uuid.UUID, kind: str) -> int:
    return count_rows(
        engine,
        "SELECT count(*) FROM ledger_entries WHERE booking_id = :b AND kind = :k",
        b=booking_id,
        k=kind,
    )


def active_rows(engine: Engine, court: uuid.UUID, start_at: datetime) -> int:
    """HELD/CONFIRMED rows (expired or not) for a court and start: the constraint's view."""
    return count_rows(
        engine,
        "SELECT count(*) FROM bookings WHERE court_id = :c AND start_at = :s"
        " AND status IN ('HELD', 'CONFIRMED')",
        c=court,
        s=start_at,
    )


def snapshot(engine: Engine) -> dict[str, str]:
    """A fingerprint of every table; equal snapshots mean no state changed (G3)."""
    out: dict[str, str] = {}
    with engine.connect() as conn:
        for table in TABLES:
            out[table] = conn.execute(
                text(
                    f"SELECT coalesce(md5(string_agg(t::text, '|' ORDER BY t.id::text)), '') "
                    f"FROM {table} t"
                )
            ).scalar_one()
    return out

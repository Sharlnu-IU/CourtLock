"""Idempotent demo data with fixed ids, so the README's examples run as written.

Run with: python -m courtlock.seed
"""

import uuid
from datetime import time

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from courtlock.config import get_settings
from courtlock.db.models import Court, Member, PeakWindow, Venue
from courtlock.db.session import make_engine, make_session_factory


def _id(suffix: str) -> uuid.UUID:
    return uuid.UUID(f"00000000-0000-4000-8000-0000000000{suffix}")


RIVERSIDE = _id("b1")
HILLTOP = _id("b2")

MEMBERS = [
    (_id("a1"), "Alice", "alice@example.com"),
    (_id("a2"), "Bob", "bob@example.com"),
    (_id("a3"), "Carol", "carol@example.com"),
    (_id("a4"), "Dave", "dave@example.com"),
]

VENUES = [
    {
        "id": RIVERSIDE,
        "name": "Riverside Sports Club",
        "timezone": "UTC",
        "opens_at": time(6, 0),
        "closes_at": time(22, 0),
        "peak_price_cents": 2000,
        "offpeak_price_cents": 1200,
        "currency": "USD",
    },
    {
        "id": HILLTOP,
        "name": "Hilltop Courts",
        "timezone": "Asia/Kolkata",
        "opens_at": time(6, 0),
        "closes_at": time(22, 0),
        "peak_price_cents": 60000,
        "offpeak_price_cents": 40000,
        "currency": "INR",
    },
]

COURTS = [
    (_id("c1"), RIVERSIDE, "Court 1", "tennis"),
    (_id("c2"), RIVERSIDE, "Court 2", "badminton"),
    (_id("c3"), HILLTOP, "Court A", "basketball"),
]

# (venue, weekdays, start, end); weekday 0 = Monday.
PEAK_RULES = [
    (RIVERSIDE, range(0, 5), time(17, 0), time(21, 0)),
    (RIVERSIDE, range(5, 7), time(8, 0), time(20, 0)),
    (HILLTOP, range(0, 5), time(18, 0), time(21, 0)),
    (HILLTOP, range(5, 7), time(7, 0), time(20, 0)),
]


def seed(session: Session) -> None:
    with session.begin():
        session.execute(insert(Venue).values(VENUES).on_conflict_do_nothing(index_elements=["id"]))
        session.execute(
            insert(Member)
            .values([{"id": i, "name": n, "email": e} for i, n, e in MEMBERS])
            .on_conflict_do_nothing(index_elements=["id"])
        )
        session.execute(
            insert(Court)
            .values(
                [
                    {"id": i, "venue_id": v, "name": n, "sport": s, "is_active": True}
                    for i, v, n, s in COURTS
                ]
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        windows = [
            {
                "id": uuid.uuid5(venue, f"peak-{weekday}-{start}"),
                "venue_id": venue,
                "weekday": weekday,
                "start_time": start,
                "end_time": end,
            }
            for venue, weekdays, start, end in PEAK_RULES
            for weekday in weekdays
        ]
        session.execute(
            insert(PeakWindow).values(windows).on_conflict_do_nothing(index_elements=["id"])
        )


def main() -> None:
    engine = make_engine(get_settings())
    with make_session_factory(engine)() as session:
        seed(session)
    engine.dispose()
    print("seed complete")


if __name__ == "__main__":
    main()

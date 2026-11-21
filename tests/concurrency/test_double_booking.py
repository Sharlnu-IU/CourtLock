"""G1: a court is never double-booked, however many requests arrive at once.

Requests go over real HTTP to TWO independent API instances (each with its own connection
pool) that share only the database, so nothing here can pass because of in-process locking.
"""

import random
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from tests.support.api import Api
from tests.support.factories import (
    active_rows,
    backdate_hold,
    booking_status,
    count_rows,
    make_booking,
    make_court,
    make_members,
    make_venue,
)
from tests.support.times import at

REQUESTS = 100


@pytest.mark.parametrize("round_number", range(5))
def test_100_simultaneous_requests_for_one_slot_produce_exactly_one_hold(
    apis: list[Api], engine: Engine, court: uuid.UUID, round_number: int
) -> None:
    members = make_members(engine, REQUESTS)
    start = at(3 + round_number)

    def attempt(index: int) -> int:
        return apis[index % len(apis)].hold(members[index], court, start).status_code

    with ThreadPoolExecutor(max_workers=REQUESTS) as pool:
        codes = list(pool.map(attempt, range(REQUESTS)))

    assert Counter(codes) == {201: 1, 409: REQUESTS - 1}
    assert active_rows(engine, court, start) == 1


def test_many_members_racing_across_many_slots_never_overlap(
    apis: list[Api], engine: Engine, court: uuid.UUID
) -> None:
    slots = [at(day, hour) for day in (3, 4) for hour in range(6, 16)]  # 20 distinct slots
    members = make_members(engine, 200)
    rng = random.Random(1234)
    wanted = [rng.choice(slots) for _ in members]

    def attempt(index: int) -> int:
        return apis[index % len(apis)].hold(members[index], court, wanted[index]).status_code

    with ThreadPoolExecutor(max_workers=100) as pool:
        codes = list(pool.map(attempt, range(len(members))))

    assert set(codes) <= {201, 409}
    assert Counter(codes)[201] == len(set(wanted))  # exactly one winner per contested slot
    assert count_rows(engine, "SELECT count(*) FROM bookings WHERE status = 'HELD'") == len(
        set(wanted)
    )
    for slot in slots:
        assert active_rows(engine, court, slot) <= 1


def test_concurrent_confirms_and_holds_on_one_slot_leave_one_owner(
    apis: list[Api], engine: Engine, court: uuid.UUID
) -> None:
    """A hold that is confirmed while others keep trying to take the slot stays the only one."""
    members = make_members(engine, 41)
    start = at(3)
    owner = members[0]
    hold_id = apis[0].hold(owner, court, start).json()["id"]

    def work(index: int) -> int:
        if index == 0:
            return apis[1].confirm(owner, hold_id).status_code
        return apis[index % 2].hold(members[index], court, start).status_code

    with ThreadPoolExecutor(max_workers=41) as pool:
        codes = list(pool.map(work, range(41)))

    assert codes[0] == 200
    assert set(codes[1:]) == {409}
    assert active_rows(engine, court, start) == 1
    assert booking_status(engine, uuid.UUID(hold_id)) == "CONFIRMED"


def test_a_member_cannot_exceed_the_cap_by_racing(
    apis: list[Api], engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    slots = [at(3 + i) for i in range(10)]

    def attempt(index: int) -> tuple[int, str]:
        response = apis[index % 2].hold(member, court, slots[index])
        return response.status_code, response.json().get("code", "")

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(attempt, range(10)))

    assert Counter(code for code, _ in results) == {201: 3, 409: 7}
    assert {name for code, name in results if code == 409} == {"booking_limit_reached"}
    assert count_rows(engine, "SELECT count(*) FROM bookings") == 3


# --- The database refuses to double-book even when the API is bypassed entirely. ---


def insert_sql() -> str:
    return (
        "INSERT INTO bookings (id, member_id, court_id, start_at, end_at, status, band,"
        " price_cents, currency, hold_expires_at, confirmed_at)"
        " VALUES (gen_random_uuid(), :m, :c, :s, :e, 'CONFIRMED', 'OFFPEAK', 100, 'USD', NULL,"
        " clock_timestamp())"
    )


def raw_insert(engine: Engine, member: uuid.UUID, court: uuid.UUID, start_offset_min: int) -> None:
    base = at(3)
    start = base + timedelta(minutes=start_offset_min)
    with engine.begin() as conn:
        conn.execute(
            text(insert_sql()),
            {"m": member, "c": court, "s": start, "e": start + timedelta(hours=1)},
        )


@pytest.mark.parametrize("offset_minutes", [0, 30, -30, 59, -59])
def test_database_rejects_overlapping_rows_written_directly(
    engine: Engine, court: uuid.UUID, member: uuid.UUID, offset_minutes: int
) -> None:
    raw_insert(engine, member, court, 0)

    with pytest.raises(IntegrityError) as caught:
        raw_insert(engine, member, court, offset_minutes)

    assert getattr(caught.value.orig, "sqlstate", None) == "23P01"


def test_adjacent_ranges_and_other_courts_are_allowed_by_the_database(
    engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    other_court = make_court(engine, make_venue(engine))
    raw_insert(engine, member, court, 0)
    raw_insert(engine, member, court, 60)  # starts exactly when the first one ends
    raw_insert(engine, member, court, -60)  # ends exactly when the first one starts
    raw_insert(engine, member, other_court, 0)
    assert count_rows(engine, "SELECT count(*) FROM bookings") == 4


def test_two_simultaneous_database_writers_cannot_both_win(
    engine: Engine, court: uuid.UUID, member: uuid.UUID, other_member: uuid.UUID
) -> None:
    """Writer 2 blocks on writer 1's uncommitted row, then fails once it commits."""
    start = at(3)
    params = {"c": court, "s": start, "e": start + timedelta(hours=1)}
    with engine.connect() as first, engine.connect() as second:
        first.execute(text(insert_sql()), {**params, "m": member})  # not committed yet

        with ThreadPoolExecutor(max_workers=1) as pool:
            blocked = pool.submit(
                lambda: second.execute(text(insert_sql()), {**params, "m": other_member})
            )
            with pytest.raises(TimeoutError):
                blocked.result(timeout=1.0)  # still waiting for writer 1
            first.commit()
            with pytest.raises(IntegrityError) as caught:
                blocked.result(timeout=10)
        second.rollback()

    assert getattr(caught.value.orig, "sqlstate", None) == "23P01"
    assert active_rows(engine, court, start) == 1


def test_a_cancelled_row_does_not_block_the_slot(
    engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    from tests.support.times import db_now

    start = db_now(engine) + timedelta(days=3)
    first = make_booking(engine, member=member, court=court, start_at=start)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE bookings SET status = 'CANCELLED', cancelled_at = clock_timestamp(),"
                " refund_percent = 0, refund_cents = 0 WHERE id = :id"
            ),
            {"id": first},
        )
    make_booking(engine, member=member, court=court, start_at=start)  # does not raise


# --- BR-6: an expired hold stops blocking for EVERY writer, even raw SQL. ---


def test_database_lets_a_writer_take_a_slot_from_an_expired_hold(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    hold_id = uuid.UUID(api.hold(member, court, at(3)).json()["id"])
    backdate_hold(engine, hold_id)

    raw_insert(engine, member, court, 0)  # no application code involved

    assert booking_status(engine, hold_id) == "EXPIRED"
    assert active_rows(engine, court, at(3)) == 1


def test_database_still_blocks_a_writer_when_the_hold_is_live(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    hold_id = uuid.UUID(api.hold(member, court, at(3)).json()["id"])

    with pytest.raises(IntegrityError) as caught:
        raw_insert(engine, member, court, 0)

    assert getattr(caught.value.orig, "sqlstate", None) == "23P01"
    assert booking_status(engine, hold_id) == "HELD"

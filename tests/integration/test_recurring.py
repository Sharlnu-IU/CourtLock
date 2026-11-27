"""POST /recurring-bookings (FR-5): atomic weekly series, counted once toward the cap.

The review's walk-through is a series over a slot whose hold has expired but was never cleaned
up. Here: test_tr1 (it succeeds), test_tr2 (a live conflict rolls everything back) and test_tr4
(a single hold over a stale hold). The raw-SQL variant (T-R3) lives in
concurrency/test_double_booking.py and the races (T-R5) in concurrency/test_recurring_races.py.
"""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine

from tests.support.api import Api
from tests.support.factories import (
    backdate_hold,
    booking_status,
    count_rows,
    make_booking,
    make_court,
    make_member,
    make_venue,
    snapshot,
)
from tests.support.faults import fault_trigger
from tests.support.times import at, iso, next_weekday


def test_twelve_week_series_books_every_week_and_charges_each(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    first = next_weekday(1, 10)  # Tuesday 10:00: off-peak every week

    response = api.series(member, court, first, 12)

    assert response.status_code == 201
    body: dict[str, Any] = response.json()
    assert set(body) == {
        "series_id",
        "court_id",
        "member_id",
        "weeks",
        "total_price_cents",
        "currency",
        "bookings",
    }
    assert (body["weeks"], body["total_price_cents"], body["currency"]) == (12, 12 * 1200, "USD")
    starts = [b["start_at"] for b in body["bookings"]]
    assert starts == [iso(first + timedelta(weeks=w)) for w in range(12)]
    assert {b["status"] for b in body["bookings"]} == {"CONFIRMED"}
    assert set(body["bookings"][0]) == {
        "id",
        "start_at",
        "end_at",
        "status",
        "price_band",
        "price_cents",
    }
    assert count_rows(engine, "SELECT count(*) FROM bookings WHERE status = 'CONFIRMED'") == 12
    assert count_rows(engine, "SELECT count(*) FROM ledger_entries WHERE kind = 'CHARGE'") == 12
    assert count_rows(engine, "SELECT count(*) FROM recurring_series") == 1


def test_each_occurrence_is_priced_by_its_own_slot(
    api: Api, court: uuid.UUID, member: uuid.UUID
) -> None:
    body = api.series(member, court, next_weekday(0, 18), 3).json()  # Monday 18:00 is peak
    assert {(b["price_band"], b["price_cents"]) for b in body["bookings"]} == {("PEAK", 2000)}
    assert body["total_price_cents"] == 3 * 2000


def test_a_whole_series_counts_once_toward_the_cap(
    api: Api, court: uuid.UUID, member: uuid.UUID
) -> None:
    assert api.series(member, court, next_weekday(1, 10), 12).status_code == 201
    assert api.hold(member, court, next_weekday(2, 10)).status_code == 201
    assert api.hold(member, court, next_weekday(3, 10)).status_code == 201  # 1 series + 2 = 3

    fourth = api.hold(member, court, next_weekday(4, 10))

    assert (fourth.status_code, fourth.json()["code"]) == (409, "booking_limit_reached")


def test_a_series_is_refused_when_the_member_is_already_at_the_cap(
    api: Api, court: uuid.UUID, member: uuid.UUID
) -> None:
    for day in (3, 4, 5):
        assert api.hold(member, court, at(day)).status_code == 201

    response = api.series(member, court, next_weekday(1, 10), 4)

    assert (response.status_code, response.json()["code"]) == (409, "booking_limit_reached")


def test_cancelling_one_week_keeps_the_series_counted_until_the_last_is_gone(
    api: Api, court: uuid.UUID, member: uuid.UUID
) -> None:
    series = api.series(member, court, next_weekday(1, 10), 3).json()
    ids = [b["id"] for b in series["bookings"]]
    api.hold(member, court, next_weekday(2, 10))
    api.hold(member, court, next_weekday(3, 10))  # series + 2 holds = 3
    assert api.hold(member, court, next_weekday(4, 10)).status_code == 409

    assert api.cancel(member, ids[0]).status_code == 200  # one week gone, series still live
    assert api.hold(member, court, next_weekday(4, 10)).status_code == 409

    assert api.cancel(member, ids[1]).status_code == 200
    assert api.cancel(member, ids[2]).status_code == 200  # no live occurrence left
    assert api.hold(member, court, next_weekday(4, 10)).status_code == 201


def test_each_occurrence_is_refunded_by_its_own_24_hour_rule(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    ids = [b["id"] for b in api.series(member, court, next_weekday(1, 10), 2).json()["bookings"]]

    cancelled = api.cancel(member, ids[0]).json()

    assert (cancelled["refund_percent"], cancelled["refund_cents"]) == (100, 1200)
    assert booking_status(engine, uuid.UUID(ids[1])) == "CONFIRMED"  # the others are untouched


@pytest.mark.parametrize("weeks", [0, 1, 13, -3])
def test_weeks_must_be_between_2_and_12(
    api: Api, court: uuid.UUID, member: uuid.UUID, weeks: int
) -> None:
    response = api.series(member, court, next_weekday(1, 10), weeks)
    assert (response.status_code, response.json()["code"]) == (422, "validation_error")


@pytest.mark.parametrize(
    ("days", "hour"),
    [(3, 5), (3, 22), (-2, 10), (200, 10)],
    ids=["before-open", "after-last-slot", "past", "beyond-horizon"],
)
def test_first_slot_must_be_a_real_future_slot(
    api: Api, court: uuid.UUID, member: uuid.UUID, days: int, hour: int
) -> None:
    response = api.series(member, court, at(days, hour), 4)
    assert (response.status_code, response.json()["code"]) == (422, "invalid_slot")


def test_a_late_week_may_fall_beyond_the_horizon_but_the_first_may_not(
    api: Api, court: uuid.UUID, member: uuid.UUID
) -> None:
    # First slot 85 days out is inside the 90-day horizon; week 12 is ~170 days out. Allowed.
    assert api.series(member, court, at(85), 12).status_code == 201


def test_unknown_court_and_member_and_missing_identity(
    api: Api, court: uuid.UUID, member: uuid.UUID
) -> None:
    start = next_weekday(1, 10)
    assert api.series(member, uuid.uuid4(), start, 3).json()["code"] == "court_not_found"
    assert api.series(uuid.uuid4(), court, start, 3).json()["code"] == "member_not_found"
    anonymous = api.client.post(
        "/recurring-bookings",
        json={"court_id": str(court), "first_start_at": iso(start), "weeks": 3},
    )
    assert (anonymous.status_code, anonymous.json()["code"]) == (401, "missing_identity")


# --- The review's walk-through: a series over an expired but never-cleaned-up hold. ---


def test_tr1_series_succeeds_over_an_expired_unswept_hold(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID, other_member: uuid.UUID
) -> None:
    first = next_weekday(1, 10)
    stale = uuid.UUID(api.hold(other_member, court, first + timedelta(weeks=1)).json()["id"])
    backdate_hold(engine, stale)  # week 2's hold timed out; no cleanup job exists to remove it

    response = api.series(member, court, first, 4)

    assert response.status_code == 201
    assert len(response.json()["bookings"]) == 4
    assert booking_status(engine, stale) == "EXPIRED"  # evicted by the database, not by a job


def test_tr2_live_conflict_is_listed_and_nothing_is_written_not_even_the_eviction(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID, other_member: uuid.UUID
) -> None:
    first = next_weekday(1, 10)
    stale = uuid.UUID(api.hold(other_member, court, first + timedelta(weeks=1)).json()["id"])
    backdate_hold(engine, stale)
    live_week = first + timedelta(weeks=3)
    blocker = make_member(engine)
    assert api.hold(blocker, court, live_week).status_code == 201  # a live hold on week 4
    before = snapshot(engine)

    response = api.series(member, court, first, 4)

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "series_conflict"
    assert body["conflicts"] == [
        {"start_at": iso(live_week)}
    ]  # only the live week, not the stale one
    assert snapshot(engine) == before  # no series, no bookings, no charges, stale hold untouched
    assert booking_status(engine, stale) == "HELD"


def test_tr4_single_hold_over_an_expired_unswept_hold_still_works(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID, other_member: uuid.UUID
) -> None:
    stale = uuid.UUID(api.hold(other_member, court, at(3)).json()["id"])
    backdate_hold(engine, stale)

    assert api.hold(member, court, at(3)).status_code == 201


# --- All or nothing (G3) ---


def test_a_taken_week_names_itself_and_books_nothing(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID, other_member: uuid.UUID
) -> None:
    first = next_weekday(1, 10)
    taken = [first + timedelta(weeks=1), first + timedelta(weeks=3)]
    for slot in taken:
        make_booking(engine, member=other_member, court=court, start_at=slot)
    before = snapshot(engine)

    response = api.series(member, court, first, 5)

    assert response.status_code == 409
    assert response.json()["conflicts"] == [{"start_at": iso(s)} for s in taken]
    assert snapshot(engine) == before


def test_series_that_fails_on_the_third_booking_leaves_nothing_behind(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    first = next_weekday(1, 10)
    before = snapshot(engine)

    with fault_trigger(
        engine,
        table="bookings",
        event="INSERT",
        when=f"NEW.start_at = '{iso(first + timedelta(weeks=2))}'::timestamptz",
    ):
        response = api.series(member, court, first, 5)

    assert response.status_code == 500
    assert snapshot(engine) == before  # the two bookings and the series row were rolled back
    assert api.series(member, court, first, 5).status_code == 201  # and it is retryable


def test_series_that_fails_while_charging_leaves_nothing_behind(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    before = snapshot(engine)

    with fault_trigger(engine, table="ledger_entries", event="INSERT"):
        response = api.series(member, court, next_weekday(1, 10), 4)

    assert response.status_code == 500
    assert snapshot(engine) == before


def test_a_retried_series_after_a_lost_response_conflicts_with_itself(
    api: Api, court: uuid.UUID, member: uuid.UUID
) -> None:
    """Known limit (no idempotency key for series): documented in LIMITATIONS.md."""
    first = next_weekday(1, 10)
    assert api.series(member, court, first, 3).status_code == 201

    retry = api.series(member, court, first, 3)

    assert (retry.status_code, retry.json()["code"]) == (409, "series_conflict")


def test_every_occurrence_keeps_the_venues_local_wall_clock_time(
    api: Api, engine: Engine, member: uuid.UUID
) -> None:
    kolkata = make_court(engine, make_venue(engine, tz="Asia/Kolkata"))
    first = at(5, 4) + timedelta(minutes=30)  # 04:30Z is 10:00 IST

    body = api.series(member, kolkata, first, 4).json()

    assert [b["start_at"][11:16] for b in body["bookings"]] == ["04:30"] * 4

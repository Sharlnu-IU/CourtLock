"""POST /bookings/{id}/cancel (FR-3): the 24 hour refund rule and idempotent cancel.

Bookings are inserted straight into the database so we can place their start time relative
to the database clock (which we cannot fake) without worrying about opening hours.
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
    ledger_count,
    make_booking,
)
from tests.support.times import at, db_now

MARGIN = timedelta(minutes=5)  # generous: covers request latency without fudging the rule


@pytest.mark.parametrize(
    ("notice", "refund_percent"),
    [
        (timedelta(days=3), 100),
        (timedelta(hours=24) + MARGIN, 100),
        (timedelta(hours=24) - MARGIN, 0),
        (timedelta(hours=2), 0),
    ],
    ids=["3-days", "just-over-24h", "just-under-24h", "2-hours"],
)
def test_refund_follows_the_24_hour_rule(
    api: Api,
    engine: Engine,
    court: uuid.UUID,
    member: uuid.UUID,
    notice: timedelta,
    refund_percent: int,
) -> None:
    booking = make_booking(
        engine, member=member, court=court, start_at=db_now(engine) + notice, price=1200
    )

    response = api.cancel(member, booking)

    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert set(body) == {
        "id",
        "status",
        "cancelled_at",
        "price_cents",
        "refund_percent",
        "refund_cents",
        "currency",
    }
    assert body["status"] == "CANCELLED"
    assert body["refund_percent"] == refund_percent
    assert body["refund_cents"] == 1200 * refund_percent // 100
    assert ledger_count(engine, booking, "REFUND") == (1 if refund_percent else 0)


def test_cancelling_twice_returns_the_original_result_and_refunds_once(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    booking = make_booking(
        engine, member=member, court=court, start_at=db_now(engine) + timedelta(days=3)
    )
    first = api.cancel(member, booking)

    second = api.cancel(member, booking)

    assert second.status_code == 200
    assert second.json() == first.json()  # same cancelled_at, same refund
    assert ledger_count(engine, booking, "REFUND") == 1


def test_a_started_booking_cannot_be_cancelled(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    booking = make_booking(
        engine, member=member, court=court, start_at=db_now(engine) - timedelta(minutes=1)
    )

    response = api.cancel(member, booking)

    assert (response.status_code, response.json()["code"]) == (409, "booking_started")
    assert booking_status(engine, booking) == "CONFIRMED"
    assert ledger_count(engine, booking, "REFUND") == 0


def test_cancelling_frees_the_slot_immediately_even_without_a_refund(
    api: Api,
    engine: Engine,
    court: uuid.UUID,
    member: uuid.UUID,
    other_member: uuid.UUID,
) -> None:
    hold_id = api.hold(member, court, at(3)).json()["id"]
    api.confirm(member, hold_id)
    assert api.hold(other_member, court, at(3)).status_code == 409

    assert api.cancel(member, hold_id).status_code == 200

    assert api.hold(other_member, court, at(3)).status_code == 201


def test_cancelling_a_live_hold_releases_it_without_moving_money(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    hold_id = api.hold(member, court, at(3)).json()["id"]

    response = api.cancel(member, hold_id)

    assert response.status_code == 200
    assert response.json()["refund_percent"] == 0
    assert response.json()["refund_cents"] == 0
    assert count_rows(engine, "SELECT count(*) FROM ledger_entries") == 0


def test_an_expired_hold_cannot_be_cancelled(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    hold_id = api.hold(member, court, at(3)).json()["id"]
    backdate_hold(engine, uuid.UUID(hold_id))

    response = api.cancel(member, hold_id)

    assert (response.status_code, response.json()["code"]) == (409, "hold_expired")


def test_someone_elses_booking_looks_like_it_does_not_exist(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID, other_member: uuid.UUID
) -> None:
    booking = make_booking(
        engine, member=member, court=court, start_at=db_now(engine) + timedelta(days=3)
    )

    response = api.cancel(other_member, booking)

    assert (response.status_code, response.json()["code"]) == (404, "booking_not_found")
    assert booking_status(engine, booking) == "CONFIRMED"


def test_unknown_booking_is_404_and_missing_identity_is_401(api: Api, member: uuid.UUID) -> None:
    unknown = api.cancel(member, uuid.uuid4())
    assert (unknown.status_code, unknown.json()["code"]) == (404, "booking_not_found")
    anonymous = api.client.post(f"/bookings/{uuid.uuid4()}/cancel")
    assert (anonymous.status_code, anonymous.json()["code"]) == (401, "missing_identity")

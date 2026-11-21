"""POST /holds (FR-1)."""

import time
import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine

from tests.support.api import Api
from tests.support.factories import (
    backdate_hold,
    booking_status,
    make_court,
    make_venue,
)
from tests.support.servers import LiveServer
from tests.support.times import at, db_now, iso, next_weekday


def test_hold_succeeds_with_locked_price_and_expiry(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    start = next_weekday(1, 10)  # a Tuesday morning: off-peak

    response = api.hold(member, court, start)

    assert response.status_code == 201
    body: dict[str, Any] = response.json()
    assert set(body) == {
        "id",
        "court_id",
        "member_id",
        "start_at",
        "end_at",
        "status",
        "price_band",
        "price_cents",
        "currency",
        "expires_at",
    }
    assert body["status"] == "HELD"
    assert body["court_id"] == str(court)
    assert body["member_id"] == str(member)
    assert body["start_at"] == iso(start)
    assert body["end_at"] == iso(start + timedelta(hours=1))
    assert (body["price_band"], body["price_cents"], body["currency"]) == ("OFFPEAK", 1200, "USD")
    expires_at = datetime.fromisoformat(body["expires_at"])
    assert timedelta(seconds=290) < expires_at - db_now(engine) <= timedelta(seconds=300)


def test_peak_hours_are_priced_higher(api: Api, court: uuid.UUID, member: uuid.UUID) -> None:
    peak_slot = next_weekday(0, 18)  # Monday 18:00 is inside Mon-Fri 17-21
    body = api.hold(member, court, peak_slot).json()
    assert (body["price_band"], body["price_cents"]) == ("PEAK", 2000)


def test_repeating_a_live_hold_returns_it_with_200(
    api: Api, court: uuid.UUID, member: uuid.UUID
) -> None:
    first = api.hold(member, court, at(3))
    second = api.hold(member, court, at(3))

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json() == first.json()


def test_slot_taken_by_someone_else_is_409(
    api: Api, court: uuid.UUID, member: uuid.UUID, other_member: uuid.UUID
) -> None:
    assert api.hold(member, court, at(3)).status_code == 201

    response = api.hold(other_member, court, at(3))

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "slot_unavailable"


def test_neighbouring_hours_and_other_courts_do_not_conflict(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID, other_member: uuid.UUID
) -> None:
    other_court = make_court(engine, make_venue(engine))
    assert api.hold(member, court, at(3, 10)).status_code == 201
    assert api.hold(other_member, court, at(3, 11)).status_code == 201  # next hour
    assert api.hold(other_member, court, at(3, 9)).status_code == 201  # previous hour
    assert (
        api.hold(other_member, other_court, at(3, 10)).status_code == 201
    )  # same time, other court


def test_expired_unswept_hold_does_not_block_the_slot(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID, other_member: uuid.UUID
) -> None:
    stale = api.hold(member, court, at(3)).json()["id"]
    backdate_hold(engine, uuid.UUID(stale))  # expired; nothing has cleaned it up

    response = api.hold(other_member, court, at(3))

    assert response.status_code == 201
    assert booking_status(engine, uuid.UUID(stale)) == "EXPIRED"


@pytest.mark.parametrize(
    "hour_offset_minutes",
    [30, 15],
)
def test_start_must_be_on_the_hour(
    api: Api, court: uuid.UUID, member: uuid.UUID, hour_offset_minutes: int
) -> None:
    response = api.hold(member, court, at(3) + timedelta(minutes=hour_offset_minutes))
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_slot"


@pytest.mark.parametrize(
    ("days", "hour"),
    [(3, 5), (3, 22), (3, 23), (-1, 10), (200, 10)],
    ids=["before-open", "after-last-slot", "late-night", "yesterday", "beyond-horizon"],
)
def test_slots_outside_hours_past_or_horizon_are_rejected(
    api: Api, court: uuid.UUID, member: uuid.UUID, days: int, hour: int
) -> None:
    response = api.hold(member, court, at(days, hour))
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_slot"


def test_start_without_utc_offset_is_a_validation_error(
    api: Api, court: uuid.UUID, member: uuid.UUID
) -> None:
    response = api.hold_raw(member, {"court_id": str(court), "start_at": "2030-01-01T10:00:00"})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_half_hour_offset_venue_uses_local_hours(
    api: Api, engine: Engine, member: uuid.UUID
) -> None:
    kolkata = make_court(engine, make_venue(engine, tz="Asia/Kolkata"))
    on_the_hour_ist = at(3, 4) + timedelta(minutes=30)  # 04:30Z == 10:00 IST
    assert api.hold(member, kolkata, on_the_hour_ist).status_code == 201
    assert api.hold(member, kolkata, at(3, 10)).status_code == 422  # 10:00Z == 15:30 IST


def test_offset_in_the_request_body_is_understood(
    api: Api, engine: Engine, member: uuid.UUID
) -> None:
    kolkata = make_court(engine, make_venue(engine, tz="Asia/Kolkata"))
    start = at(3, 4) + timedelta(minutes=30)
    day = start.date().isoformat()
    response = api.hold_raw(member, {"court_id": str(kolkata), "start_at": f"{day}T10:00:00+05:30"})
    assert response.status_code == 201
    assert response.json()["start_at"] == iso(start)


def test_unknown_court_is_404(api: Api, member: uuid.UUID) -> None:
    response = api.hold(member, uuid.uuid4(), at(3))
    assert (response.status_code, response.json()["code"]) == (404, "court_not_found")


def test_unknown_member_is_404(api: Api, court: uuid.UUID) -> None:
    response = api.hold(uuid.uuid4(), court, at(3))
    assert (response.status_code, response.json()["code"]) == (404, "member_not_found")


@pytest.mark.parametrize("header", [None, "not-a-uuid"])
def test_missing_or_malformed_identity_is_401(
    api: Api, court: uuid.UUID, header: str | None
) -> None:
    headers = {} if header is None else {"X-Member-Id": header}
    response = api.client.post(
        "/holds", headers=headers, json={"court_id": str(court), "start_at": iso(at(3))}
    )
    assert (response.status_code, response.json()["code"]) == (401, "missing_identity")


def test_member_may_hold_three_active_future_bookings_not_four(
    api: Api, court: uuid.UUID, member: uuid.UUID
) -> None:
    assert [api.hold(member, court, at(d)).status_code for d in (3, 4, 5)] == [201, 201, 201]

    fourth = api.hold(member, court, at(6))

    assert fourth.status_code == 409
    assert fourth.json()["code"] == "booking_limit_reached"


def test_releasing_a_hold_frees_a_place_under_the_cap(
    api: Api, court: uuid.UUID, member: uuid.UUID
) -> None:
    holds = [api.hold(member, court, at(d)).json()["id"] for d in (3, 4, 5)]
    assert api.hold(member, court, at(6)).status_code == 409

    assert api.cancel(member, holds[0]).status_code == 200

    assert api.hold(member, court, at(6)).status_code == 201


def test_expired_holds_do_not_count_toward_the_cap(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    holds = [api.hold(member, court, at(d)).json()["id"] for d in (3, 4, 5)]
    backdate_hold(engine, uuid.UUID(holds[0]))

    assert api.hold(member, court, at(6)).status_code == 201


@pytest.mark.slow
def test_a_hold_really_expires_after_its_ttl(
    short_ttl_server: LiveServer,
    court: uuid.UUID,
    member: uuid.UUID,
    other_member: uuid.UUID,
) -> None:
    """End to end in real time, with a one-second TTL and no cleanup job at all."""
    quick = Api(short_ttl_server.base_url)
    hold_id = quick.hold(member, court, at(3)).json()["id"]
    assert quick.hold(other_member, court, at(3)).status_code == 409  # still held

    time.sleep(1.3)

    assert quick.confirm(member, hold_id).json()["code"] == "hold_expired"
    assert quick.hold(other_member, court, at(3)).status_code == 201
    quick.close()

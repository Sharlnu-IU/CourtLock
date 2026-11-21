"""GET /courts/{id}/availability (FR-4)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine

from tests.support.api import Api
from tests.support.factories import backdate_hold, make_court, make_venue
from tests.support.times import at, iso, next_weekday


def slots_by_start(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {slot["start_at"]: slot for slot in body["slots"]}


def test_lists_every_slot_of_the_day_with_price_and_status(api: Api, court: uuid.UUID) -> None:
    monday = next_weekday(0, 6)
    response = api.availability(court, **{"from": monday.date().isoformat()})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"court_id", "timezone", "slots"}
    assert body["timezone"] == "UTC"
    slots = body["slots"]
    assert len(slots) == 16  # 06:00-22:00
    assert slots[0]["start_at"] == iso(monday)
    assert set(slots[0]) == {
        "start_at",
        "end_at",
        "status",
        "price_band",
        "price_cents",
        "currency",
    }
    assert {s["status"] for s in slots} == {"AVAILABLE"}
    bands = {s["start_at"]: s["price_band"] for s in slots}
    assert bands[iso(monday.replace(hour=16))] == "OFFPEAK"
    assert bands[iso(monday.replace(hour=17))] == "PEAK"
    assert bands[iso(monday.replace(hour=21))] == "OFFPEAK"


def test_shows_held_and_booked_slots_without_naming_anyone(
    api: Api, court: uuid.UUID, member: uuid.UUID, other_member: uuid.UUID
) -> None:
    day = at(3).date().isoformat()
    api.hold(member, court, at(3, 10))
    booked = api.hold(other_member, court, at(3, 11)).json()["id"]
    api.confirm(other_member, booked)

    response = api.availability(court, **{"from": day})

    slots = slots_by_start(response.json())
    assert slots[iso(at(3, 10))]["status"] == "HELD"
    assert slots[iso(at(3, 11))]["status"] == "BOOKED"
    assert slots[iso(at(3, 12))]["status"] == "AVAILABLE"
    assert str(member) not in response.text
    assert str(other_member) not in response.text


def test_an_expired_unswept_hold_shows_as_available(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    hold_id = api.hold(member, court, at(3, 10)).json()["id"]
    backdate_hold(engine, uuid.UUID(hold_id))

    slots = slots_by_start(api.availability(court, **{"from": at(3).date().isoformat()}).json())

    assert slots[iso(at(3, 10))]["status"] == "AVAILABLE"


def test_cancelled_booking_shows_as_available_again(
    api: Api, court: uuid.UUID, member: uuid.UUID
) -> None:
    hold_id = api.hold(member, court, at(3, 10)).json()["id"]
    api.confirm(member, hold_id)
    api.cancel(member, hold_id)

    slots = slots_by_start(api.availability(court, **{"from": at(3).date().isoformat()}).json())

    assert slots[iso(at(3, 10))]["status"] == "AVAILABLE"


def test_defaults_to_today_and_omits_slots_that_have_started(api: Api, court: uuid.UUID) -> None:
    body = api.availability(court).json()
    now = datetime.now(UTC)
    assert all(
        datetime.fromisoformat(s["start_at"]) > now - timedelta(minutes=1) for s in body["slots"]
    )


def test_date_range_is_from_inclusive_to_exclusive(api: Api, court: uuid.UUID) -> None:
    first = at(3).date()
    one_day = api.availability(court, **{"from": first.isoformat()}).json()["slots"]
    two_days = api.availability(
        court, **{"from": first.isoformat(), "to": (first + timedelta(days=2)).isoformat()}
    ).json()["slots"]
    assert (len(one_day), len(two_days)) == (16, 32)


def test_slots_follow_the_venues_local_time(api: Api, engine: Engine) -> None:
    kolkata = make_court(engine, make_venue(engine, tz="Asia/Kolkata", currency="INR"))
    body = api.availability(kolkata, **{"from": at(3).date().isoformat()}).json()
    assert body["timezone"] == "Asia/Kolkata"
    assert all(datetime.fromisoformat(s["start_at"]).minute == 30 for s in body["slots"])
    assert body["slots"][0]["currency"] == "INR"


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2030-01-01", "to": "2030-01-01"},
        {"from": "2030-01-05", "to": "2030-01-01"},
        {"from": "2030-01-01", "to": "2030-01-20"},
        {"from": "not-a-date"},
    ],
    ids=["empty-range", "reversed", "over-14-days", "bad-date"],
)
def test_bad_ranges_are_validation_errors(
    api: Api, court: uuid.UUID, params: dict[str, str]
) -> None:
    response = api.availability(court, **params)
    assert (response.status_code, response.json()["code"]) == (422, "validation_error")


def test_unknown_court_is_404(api: Api) -> None:
    response = api.availability(uuid.uuid4())
    assert (response.status_code, response.json()["code"]) == (404, "court_not_found")


def test_needs_no_identity(api: Api, court: uuid.UUID) -> None:
    assert api.client.get(f"/courts/{court}/availability").status_code == 200

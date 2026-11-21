"""POST /holds/{id}/confirm (FR-2) and the ledger it writes."""

import uuid
from typing import Any

from sqlalchemy import Engine, text

from tests.support.api import Api
from tests.support.factories import (
    backdate_hold,
    booking_status,
    count_rows,
    ledger_count,
    make_court,
    make_venue,
)
from tests.support.times import at, iso


def test_confirm_makes_a_booking_with_the_same_id_and_one_charge(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    hold = api.hold(member, court, at(3)).json()

    response = api.confirm(member, hold["id"])

    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    assert set(body) == {
        "id",
        "court_id",
        "member_id",
        "status",
        "confirmed_at",
        "start_at",
        "end_at",
        "price_band",
        "price_cents",
        "currency",
    }
    assert body["id"] == hold["id"]
    assert body["status"] == "CONFIRMED"
    assert body["start_at"] == iso(at(3))
    assert "idempotent-replay" not in response.headers
    assert ledger_count(engine, uuid.UUID(hold["id"]), "CHARGE") == 1
    with engine.connect() as conn:
        amount = conn.execute(
            text("SELECT amount_cents FROM ledger_entries WHERE booking_id = :b"),
            {"b": hold["id"]},
        ).scalar_one()
    assert amount == hold["price_cents"]


def test_repeating_confirm_returns_the_same_booking_and_never_charges_twice(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    hold_id = api.hold(member, court, at(3)).json()["id"]
    first = api.confirm(member, hold_id)

    replay = api.confirm(member, hold_id)

    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.headers["idempotent-replay"] == "true"
    assert ledger_count(engine, uuid.UUID(hold_id), "CHARGE") == 1


def test_confirming_an_expired_hold_fails_even_if_the_slot_is_free(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    hold_id = uuid.UUID(api.hold(member, court, at(3)).json()["id"])
    backdate_hold(engine, hold_id)

    response = api.confirm(member, hold_id)

    assert (response.status_code, response.json()["code"]) == (409, "hold_expired")
    assert booking_status(engine, hold_id) != "CONFIRMED"
    assert ledger_count(engine, hold_id, "CHARGE") == 0


def test_someone_elses_hold_looks_like_it_does_not_exist(
    api: Api, court: uuid.UUID, member: uuid.UUID, other_member: uuid.UUID
) -> None:
    hold_id = api.hold(member, court, at(3)).json()["id"]

    response = api.confirm(other_member, hold_id)

    assert (response.status_code, response.json()["code"]) == (404, "hold_not_found")


def test_unknown_hold_is_404(api: Api, member: uuid.UUID) -> None:
    response = api.confirm(member, uuid.uuid4())
    assert (response.status_code, response.json()["code"]) == (404, "hold_not_found")


def test_missing_identity_is_401(api: Api) -> None:
    response = api.client.post(f"/holds/{uuid.uuid4()}/confirm")
    assert (response.status_code, response.json()["code"]) == (401, "missing_identity")


def test_a_cancelled_booking_cannot_be_confirmed(
    api: Api, court: uuid.UUID, member: uuid.UUID
) -> None:
    hold_id = api.hold(member, court, at(3)).json()["id"]
    assert api.cancel(member, hold_id).status_code == 200  # releases the hold

    response = api.confirm(member, hold_id)

    assert (response.status_code, response.json()["code"]) == (409, "invalid_state")


def test_price_is_locked_at_hold_time(api: Api, engine: Engine, member: uuid.UUID) -> None:
    venue = make_venue(engine, offpeak=1200)
    court = make_court(engine, venue)
    hold = api.hold(member, court, at(3)).json()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE venues SET offpeak_price_cents = 9900 WHERE id = :v"), {"v": venue}
        )

    confirmed = api.confirm(member, hold["id"]).json()

    assert confirmed["price_cents"] == 1200
    with engine.connect() as conn:
        charged = conn.execute(
            text("SELECT amount_cents FROM ledger_entries WHERE booking_id = :b"),
            {"b": hold["id"]},
        ).scalar_one()
    assert charged == 1200


def test_a_free_booking_writes_no_ledger_row(api: Api, engine: Engine, member: uuid.UUID) -> None:
    court = make_court(engine, make_venue(engine, peak=0, offpeak=0))
    hold_id = api.hold(member, court, at(3)).json()["id"]

    assert api.confirm(member, hold_id).status_code == 200

    assert count_rows(engine, "SELECT count(*) FROM ledger_entries") == 0

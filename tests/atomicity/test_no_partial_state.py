"""G3: a failed multi-step operation leaves no partial state.

We make one step of an operation fail with a temporary database trigger, call the endpoint,
and require every table to be byte-for-byte what it was before the call.
"""

import threading
import time
import uuid
from datetime import timedelta

from sqlalchemy import Engine, text

from tests.support.api import Api
from tests.support.factories import (
    backdate_hold,
    booking_status,
    count_rows,
    ledger_count,
    make_booking,
    snapshot,
)
from tests.support.faults import fault_trigger
from tests.support.times import at, db_now


def test_confirm_that_fails_while_charging_leaves_the_hold_untouched(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    hold_id = uuid.UUID(api.hold(member, court, at(3)).json()["id"])
    before = snapshot(engine)

    with fault_trigger(engine, table="ledger_entries", event="INSERT"):
        response = api.confirm(member, hold_id)

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert "injected" not in response.text  # nothing internal leaks
    assert snapshot(engine) == before
    assert booking_status(engine, hold_id) == "HELD"  # not half-confirmed
    assert api.confirm(member, hold_id).status_code == 200  # and it is still retryable


def test_cancel_that_fails_while_refunding_leaves_the_booking_confirmed(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    booking = make_booking(
        engine, member=member, court=court, start_at=db_now(engine) + timedelta(days=3)
    )
    before = snapshot(engine)

    with fault_trigger(engine, table="ledger_entries", event="INSERT"):
        response = api.cancel(member, booking)

    assert response.status_code == 500
    assert snapshot(engine) == before
    assert booking_status(engine, booking) == "CONFIRMED"
    assert ledger_count(engine, booking, "REFUND") == 0
    assert api.cancel(member, booking).status_code == 200  # a later attempt works


def test_cancel_that_fails_while_marking_cancelled_writes_no_refund(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    booking = make_booking(
        engine, member=member, court=court, start_at=db_now(engine) + timedelta(days=3)
    )
    before = snapshot(engine)

    with fault_trigger(engine, table="bookings", event="UPDATE", when="NEW.status = 'CANCELLED'"):
        response = api.cancel(member, booking)

    assert response.status_code == 500
    assert snapshot(engine) == before


def test_confirm_that_fails_while_marking_confirmed_writes_no_charge(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    hold_id = api.hold(member, court, at(3)).json()["id"]
    before = snapshot(engine)

    with fault_trigger(engine, table="bookings", event="UPDATE", when="NEW.status = 'CONFIRMED'"):
        response = api.confirm(member, hold_id)

    assert response.status_code == 500
    assert snapshot(engine) == before


def test_hold_that_fails_after_evicting_an_expired_hold_undoes_the_eviction(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID, other_member: uuid.UUID
) -> None:
    """The trigger's eviction and the new row are one unit: if the insert dies, nothing moves."""
    stale = uuid.UUID(api.hold(member, court, at(3)).json()["id"])
    backdate_hold(engine, stale)
    before = snapshot(engine)

    with fault_trigger(engine, table="bookings", event="INSERT"):
        response = api.hold(other_member, court, at(3))

    assert response.status_code == 500
    assert snapshot(engine) == before
    assert booking_status(engine, stale) == "HELD"  # still the original, unevicted row
    assert api.hold(other_member, court, at(3)).status_code == 201  # and the slot is takeable


def test_a_killed_database_connection_mid_confirm_changes_nothing(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    """Simulates a crash after the UPDATE and before the ledger insert completes."""
    hold_id = api.hold(member, court, at(3)).json()["id"]
    before = snapshot(engine)
    results: list[int] = []

    with fault_trigger(engine, table="ledger_entries", event="INSERT", sleep_seconds=8):
        worker = threading.Thread(
            target=lambda: results.append(api.confirm(member, hold_id).status_code)
        )
        worker.start()
        deadline = time.monotonic() + 10
        killed = 0
        while not killed and time.monotonic() < deadline:
            with engine.begin() as conn:
                killed = conn.execute(
                    text(
                        "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity"
                        " WHERE state = 'active' AND query LIKE 'INSERT INTO ledger_entries%'"
                    )
                ).scalar_one()
            time.sleep(0.05)
        worker.join(timeout=15)

    assert killed == 1
    assert results == [500]
    assert snapshot(engine) == before
    assert count_rows(engine, "SELECT count(*) FROM ledger_entries") == 0

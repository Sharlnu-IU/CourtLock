"""G2: a client retrying a timed-out confirm never ends up with two bookings (or two charges)."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from tests.support.api import Api
from tests.support.factories import booking_status, count_rows, ledger_count, make_booking
from tests.support.faults import fault_trigger
from tests.support.times import at, db_now

RETRIES = 20


def test_the_brief_scenario_confirm_times_out_and_the_client_retries(
    servers: list[Any], engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    """The server is slow, the client gives up, then retries. One booking, one charge."""
    api = Api(servers[0].base_url)
    impatient = Api(servers[0].base_url, timeout=0.4)
    hold_id = api.hold(member, court, at(3)).json()["id"]

    with fault_trigger(engine, table="ledger_entries", event="INSERT", sleep_seconds=1.5):
        with pytest.raises(httpx.ReadTimeout):
            impatient.confirm(member, hold_id)  # the server is still working on it
        retry = api.confirm(member, hold_id)  # waits for the first attempt to finish

    assert retry.status_code == 200
    assert retry.json()["status"] == "CONFIRMED"
    assert retry.headers["idempotent-replay"] == "true"
    assert booking_status(engine, uuid.UUID(hold_id)) == "CONFIRMED"
    assert ledger_count(engine, uuid.UUID(hold_id), "CHARGE") == 1
    assert count_rows(engine, "SELECT count(*) FROM bookings") == 1
    api.close()
    impatient.close()


def test_confirming_20_times_one_after_another(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    hold_id = api.hold(member, court, at(3)).json()["id"]

    responses = [api.confirm(member, hold_id) for _ in range(RETRIES)]

    assert {r.status_code for r in responses} == {200}
    assert len({r.text for r in responses}) == 1  # byte-identical bodies
    assert ledger_count(engine, uuid.UUID(hold_id), "CHARGE") == 1
    assert count_rows(engine, "SELECT count(*) FROM bookings WHERE status = 'CONFIRMED'") == 1


def test_confirming_20_times_at_once_from_two_instances(
    apis: list[Api], engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    hold_id = apis[0].hold(member, court, at(3)).json()["id"]

    with ThreadPoolExecutor(max_workers=RETRIES) as pool:
        responses = list(pool.map(lambda i: apis[i % 2].confirm(member, hold_id), range(RETRIES)))

    assert {r.status_code for r in responses} == {200}
    assert len({r.text for r in responses}) == 1
    replays = [r for r in responses if r.headers.get("idempotent-replay") == "true"]
    assert len(replays) == RETRIES - 1  # exactly one request did the work
    assert ledger_count(engine, uuid.UUID(hold_id), "CHARGE") == 1
    assert count_rows(engine, "SELECT count(*) FROM bookings") == 1


def test_cancelling_20_times_at_once_refunds_once(
    apis: list[Api], engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    booking = make_booking(
        engine, member=member, court=court, start_at=db_now(engine) + timedelta(days=3)
    )

    with ThreadPoolExecutor(max_workers=RETRIES) as pool:
        responses = list(pool.map(lambda i: apis[i % 2].cancel(member, booking), range(RETRIES)))

    assert {r.status_code for r in responses} == {200}
    assert len({r.text for r in responses}) == 1  # same cancelled_at, same refund
    assert ledger_count(engine, booking, "REFUND") == 1


def test_a_lost_hold_response_is_safe_to_retry(
    apis: list[Api], engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    with ThreadPoolExecutor(max_workers=RETRIES) as pool:
        responses = list(pool.map(lambda i: apis[i % 2].hold(member, court, at(3)), range(RETRIES)))

    assert sorted({r.status_code for r in responses}) == [200, 201]
    assert sum(r.status_code == 201 for r in responses) == 1
    assert len({r.json()["id"] for r in responses}) == 1
    assert count_rows(engine, "SELECT count(*) FROM bookings") == 1


def test_the_ledger_itself_refuses_a_second_charge_or_refund(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    """Even if application code were wrong, the database cannot hold two charges."""
    hold_id = api.hold(member, court, at(3)).json()["id"]
    api.confirm(member, hold_id)

    with pytest.raises(IntegrityError) as caught, engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ledger_entries (id, booking_id, member_id, kind, amount_cents,"
                " currency) VALUES (gen_random_uuid(), :b, :m, 'CHARGE', 1200, 'USD')"
            ),
            {"b": hold_id, "m": member},
        )

    assert getattr(caught.value.orig, "sqlstate", None) == "23505"
    assert ledger_count(engine, uuid.UUID(hold_id), "CHARGE") == 1

"""The README's walkthrough, as a black box over HTTP, against the seeded demo data.

Uses only what a third party sees: the four paths, the documented headers and bodies, and the
fixed demo ids from the README's seed table.
"""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import Engine

from courtlock.db.session import make_session_factory
from courtlock.seed import seed
from tests.support.servers import LiveServer

ALICE = "00000000-0000-4000-8000-0000000000a1"
BOB = "00000000-0000-4000-8000-0000000000a2"
COURT_1 = "00000000-0000-4000-8000-0000000000c1"
HILLTOP_COURT = "00000000-0000-4000-8000-0000000000c3"


@pytest.fixture
def client(engine: Engine, servers: list[LiveServer]) -> httpx.Client:
    with make_session_factory(engine)() as session:
        seed(session)
    return httpx.Client(base_url=servers[0].base_url, timeout=30)


@pytest.fixture
def start() -> str:
    day = datetime.now(UTC) + timedelta(days=2)
    return f"{day:%Y-%m-%d}T10:00:00Z"  # the README's START


def test_readme_walkthrough(client: httpx.Client, start: str) -> None:
    day = start.split("T")[0]

    # 1. availability needs no identity
    availability = client.get(f"/courts/{COURT_1}/availability", params={"from": day})
    assert availability.status_code == 200
    assert {s["start_at"]: s["status"] for s in availability.json()["slots"]}[start] == "AVAILABLE"

    # 2. hold (201), retry (200, same hold), then Bob is refused
    body = {"court_id": COURT_1, "start_at": start}
    hold = client.post("/holds", json=body, headers={"X-Member-Id": ALICE})
    assert hold.status_code == 201
    assert hold.json()["status"] == "HELD"
    hold_id = hold.json()["id"]
    again = client.post("/holds", json=body, headers={"X-Member-Id": ALICE})
    assert (again.status_code, again.json()["id"]) == (200, hold_id)
    bob = client.post("/holds", json=body, headers={"X-Member-Id": BOB})
    assert bob.status_code == 409
    assert bob.headers["content-type"].startswith("application/problem+json")
    assert bob.json()["code"] == "slot_unavailable"

    # 3. confirm, then replay it
    confirmed = client.post(f"/holds/{hold_id}/confirm", headers={"X-Member-Id": ALICE})
    assert (confirmed.status_code, confirmed.json()["status"]) == (200, "CONFIRMED")
    replay = client.post(f"/holds/{hold_id}/confirm", headers={"X-Member-Id": ALICE})
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replay"] == "true"
    assert replay.json() == confirmed.json()

    # 4. cancel two days ahead: full refund; cancelling again returns the same result
    cancelled = client.post(f"/bookings/{hold_id}/cancel", headers={"X-Member-Id": ALICE})
    assert cancelled.status_code == 200
    assert cancelled.json()["refund_percent"] == 100
    assert cancelled.json()["refund_cents"] == cancelled.json()["price_cents"]
    twice = client.post(f"/bookings/{hold_id}/cancel", headers={"X-Member-Id": ALICE})
    assert twice.json() == cancelled.json()

    # the slot is bookable again
    assert client.post("/holds", json=body, headers={"X-Member-Id": BOB}).status_code == 201


def test_seeded_hilltop_court_uses_its_own_time_zone(client: httpx.Client) -> None:
    day = (datetime.now(UTC) + timedelta(days=2)).date().isoformat()
    slots = client.get(f"/courts/{HILLTOP_COURT}/availability", params={"from": day}).json()
    assert slots["timezone"] == "Asia/Kolkata"
    assert slots["slots"][0]["currency"] == "INR"
    assert slots["slots"][0]["start_at"].endswith("00:30:00Z")  # 06:00 IST


def test_seed_is_idempotent(client: httpx.Client, engine: Engine) -> None:
    with make_session_factory(engine)() as session:
        seed(session)
        seed(session)
    assert client.get(f"/courts/{uuid.UUID(COURT_1)}/availability").status_code == 200

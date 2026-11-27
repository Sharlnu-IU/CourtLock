"""GET /courts (FR-7)."""

import uuid

from sqlalchemy import Engine

from tests.support.api import Api
from tests.support.factories import make_court, make_inactive_court, make_venue


def test_lists_active_courts_with_their_venue(api: Api, engine: Engine) -> None:
    venue = make_venue(engine, tz="Asia/Kolkata")
    first = make_court(engine, venue, "Court A")
    second = make_court(engine, venue, "Court B")
    make_inactive_court(engine, venue)

    response = api.courts()

    assert response.status_code == 200
    courts = response.json()["courts"]
    assert [c["id"] for c in courts] == [str(first), str(second)]  # inactive court is hidden
    assert set(courts[0]) == {"id", "name", "sport", "venue_id", "venue_name", "timezone"}
    assert courts[0]["venue_id"] == str(venue)
    assert courts[0]["timezone"] == "Asia/Kolkata"


def test_empty_when_there_are_no_courts(api: Api) -> None:
    assert api.courts().json() == {"courts": []}


def test_listed_court_ids_work_with_availability(api: Api, court: uuid.UUID) -> None:
    listed = api.courts().json()["courts"][0]["id"]
    assert api.client.get(f"/courts/{listed}/availability").status_code == 200


def test_needs_no_identity(api: Api, court: uuid.UUID) -> None:
    assert api.client.get("/courts").status_code == 200

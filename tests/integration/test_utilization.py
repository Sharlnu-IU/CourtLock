"""GET /staff/utilization (FR-6, BR-18): confirmed booked hours / bookable hours per week."""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine

from tests.support.api import Api
from tests.support.factories import (
    make_booking,
    make_court,
    make_inactive_court,
    make_member,
    make_venue,
)
from tests.support.times import at, next_weekday

UTC_WEEK_HOURS = 16 * 7  # a UTC venue open 06:00-22:00 offers 16 slots a day


def iso_week(monday: date) -> str:
    year, week, _ = monday.isocalendar()
    return f"{year}-W{week:02d}"


def future_monday() -> datetime:
    return next_weekday(0, 6, min_days_ahead=10)  # Monday 06:00Z, the first slot of its week


def book(
    engine: Engine,
    member: uuid.UUID,
    court: uuid.UUID,
    start: datetime,
    status: str = "CONFIRMED",
    band: str = "OFFPEAK",
) -> None:
    expires = datetime.now(UTC) + timedelta(hours=1) if status == "HELD" else None
    make_booking(
        engine,
        member=member,
        court=court,
        start_at=start,
        status=status,
        band=band,
        hold_expires_at=expires,
    )


def test_counts_only_confirmed_bookings_in_the_week(
    api: Api, engine: Engine, member: uuid.UUID
) -> None:
    venue = make_venue(engine)
    busy = make_court(engine, venue, "Court 1")
    make_court(engine, venue, "Court 2")  # stays empty
    monday = future_monday()

    for hour in (8, 9, 10):  # three off-peak hours
        book(engine, member, busy, monday + timedelta(days=1, hours=hour - 6))
    for hour in (18, 19):  # two peak hours
        book(engine, member, busy, monday + timedelta(days=2, hours=hour - 6), band="PEAK")
    book(engine, member, busy, monday + timedelta(days=3), status="CANCELLED")  # not counted
    book(engine, member, busy, monday + timedelta(days=3, hours=1), status="HELD")  # a live hold
    book(engine, member, busy, monday + timedelta(days=4), status="EXPIRED")  # not counted
    book(engine, member, busy, monday + timedelta(days=-1, hours=3))  # previous week
    book(engine, member, busy, monday + timedelta(days=7, hours=3))  # next week

    response = api.utilization(week=iso_week(monday.date()), venue_id=str(venue))

    assert response.status_code == 200
    body = response.json()
    assert body["week"] == iso_week(monday.date())
    assert body["week_start"] == monday.date().isoformat()
    assert body["week_end"] == (monday.date() + timedelta(days=7)).isoformat()  # exclusive
    [venue_row] = body["venues"]
    assert venue_row["timezone"] == "UTC"
    courts = {c["name"]: c for c in venue_row["courts"]}
    assert courts["Court 1"] == {
        "court_id": str(busy),
        "name": "Court 1",
        "booked_hours": 5,
        "bookable_hours": UTC_WEEK_HOURS,
        "utilization_pct": 4.5,  # 5 / 112
        "peak_hours": 2,
        "offpeak_hours": 3,
    }
    assert courts["Court 2"]["booked_hours"] == 0
    assert courts["Court 2"]["utilization_pct"] == 0.0
    assert (venue_row["booked_hours"], venue_row["bookable_hours"]) == (5, 2 * UTC_WEEK_HOURS)
    assert venue_row["utilization_pct"] == 2.2  # 5 / 224


def test_week_runs_monday_to_sunday_in_the_venues_time(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    monday = future_monday()
    book(engine, member, court, monday - timedelta(hours=9))  # Sunday 21:00, last week
    book(engine, member, court, monday)  # Monday 06:00: first slot of the week
    book(engine, member, court, monday + timedelta(days=6, hours=15))  # Sunday 21:00: last slot
    book(engine, member, court, monday + timedelta(days=7))  # next Monday 06:00

    this_week = api.utilization(week=iso_week(monday.date())).json()["venues"][0]
    assert this_week["booked_hours"] == 2


def test_a_booking_belongs_to_the_week_of_its_local_date_not_its_utc_date(
    api: Api, engine: Engine, member: uuid.UUID
) -> None:
    la = ZoneInfo("America/Los_Angeles")
    kolkata = ZoneInfo("Asia/Kolkata")
    la_court = make_court(engine, make_venue(engine, tz="America/Los_Angeles", closes=time(23)))
    in_court = make_court(engine, make_venue(engine, tz="Asia/Kolkata", closes=time(23)), "Court I")
    monday = future_monday().date()
    last_sunday = monday - timedelta(days=1)

    # Sunday 20:00 in Los Angeles is already Monday in UTC, but is still LAST week's Sunday.
    sunday_evening = datetime.combine(last_sunday, time(20)).replace(tzinfo=la).astimezone(UTC)
    assert sunday_evening.date() == monday
    book(engine, member, la_court, sunday_evening)
    # Monday 00:00 in Kolkata is still Sunday in UTC, but is THIS week's Monday.
    monday_midnight = datetime.combine(monday, time(0)).replace(tzinfo=kolkata).astimezone(UTC)
    assert monday_midnight.date() == last_sunday
    book(engine, member, in_court, monday_midnight)

    rows = {
        v["timezone"]: v["booked_hours"]
        for v in api.utilization(week=iso_week(monday)).json()["venues"]
    }
    assert rows == {"America/Los_Angeles": 0, "Asia/Kolkata": 1}
    previous = {
        v["timezone"]: v["booked_hours"]
        for v in api.utilization(week=iso_week(last_sunday)).json()["venues"]
    }
    assert previous == {"America/Los_Angeles": 1, "Asia/Kolkata": 0}


@pytest.mark.parametrize(
    ("week", "bookable_per_court"),
    [
        ("2027-W10", 6 * 6 + 5),  # Mon 8 - Sun 14 Mar 2027: the clock jumps 02:00 -> 03:00
        ("2027-W44", 7 * 6),  # Mon 1 - Sun 7 Nov 2027: the repeated hour is offered once
        ("2027-W20", 7 * 6),  # an ordinary week
    ],
)
def test_clock_change_weeks_use_the_slots_that_really_exist(
    api: Api, engine: Engine, week: str, bookable_per_court: int
) -> None:
    make_court(engine, make_venue(engine, tz="America/New_York", opens=time(0), closes=time(6)))

    [venue] = api.utilization(week=week).json()["venues"]

    assert venue["courts"][0]["bookable_hours"] == bookable_per_court


def test_inactive_courts_are_left_out_entirely(api: Api, engine: Engine, member: uuid.UUID) -> None:
    venue = make_venue(engine)
    active = make_court(engine, venue)
    retired = make_inactive_court(engine, venue)
    monday = future_monday()
    book(engine, member, active, monday)
    book(engine, member, retired, monday)

    [row] = api.utilization(week=iso_week(monday.date())).json()["venues"]

    assert [c["court_id"] for c in row["courts"]] == [str(active)]
    assert (row["booked_hours"], row["bookable_hours"]) == (1, UTC_WEEK_HOURS)


def test_lists_every_venue_by_name_or_just_the_one_asked_for(api: Api, engine: Engine) -> None:
    first, second = make_venue(engine), make_venue(engine)
    make_court(engine, first)
    make_court(engine, second)
    week = iso_week(future_monday().date())

    everything = api.utilization(week=week).json()["venues"]
    one = api.utilization(week=week, venue_id=str(second)).json()["venues"]

    assert len(everything) == 2
    assert [v["name"] for v in everything] == sorted(v["name"] for v in everything)
    assert [v["venue_id"] for v in one] == [str(second)]


def test_a_venue_with_no_courts_reports_zero_not_an_error(api: Api, engine: Engine) -> None:
    make_venue(engine)
    [row] = api.utilization(week=iso_week(future_monday().date())).json()["venues"]
    assert (row["booked_hours"], row["bookable_hours"], row["utilization_pct"]) == (0, 0, 0.0)
    assert row["courts"] == []


def test_series_bookings_show_up_week_by_week(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    first = next_weekday(1, 10, min_days_ahead=10)
    assert api.series(member, court, first, 3).status_code == 201

    for week in range(3):
        monday = (first + timedelta(weeks=week)).date() - timedelta(days=1)
        [row] = api.utilization(week=iso_week(monday)).json()["venues"]
        assert row["booked_hours"] == 1
    after = (first + timedelta(weeks=3)).date() - timedelta(days=1)
    assert api.utilization(week=iso_week(after)).json()["venues"][0]["booked_hours"] == 0


def test_cancelling_a_booking_removes_it_from_utilization(
    api: Api, engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    slot = at(5, 10)
    hold = api.hold(member, court, slot).json()["id"]
    api.confirm(member, hold)
    week = iso_week(slot.date() - timedelta(days=slot.weekday()))
    assert api.utilization(week=week).json()["venues"][0]["booked_hours"] == 1

    api.cancel(member, hold)

    assert api.utilization(week=week).json()["venues"][0]["booked_hours"] == 0


def test_the_peak_split_uses_the_band_locked_at_booking_time(
    api: Api, engine: Engine, court: uuid.UUID
) -> None:
    owner = make_member(engine)
    slot = next_weekday(0, 18, min_days_ahead=10)  # Monday 18:00: a peak slot
    hold = api.hold(owner, court, slot).json()["id"]
    api.confirm(owner, hold)

    [row] = api.utilization(week=iso_week(slot.date())).json()["venues"]

    assert (row["courts"][0]["peak_hours"], row["courts"][0]["offpeak_hours"]) == (1, 0)


# --- access and validation ---


@pytest.mark.parametrize("key", [None, "wrong-key", ""])
def test_staff_key_is_required(api: Api, key: str | None) -> None:
    response = api.utilization(key=key, week="2026-W48")
    assert (response.status_code, response.json()["code"]) == (401, "invalid_staff_key")


def test_a_member_identity_is_not_a_staff_key(api: Api, member: uuid.UUID) -> None:
    response = api.client.get(
        "/staff/utilization", params={"week": "2026-W48"}, headers={"X-Member-Id": str(member)}
    )
    assert response.status_code == 401


@pytest.mark.parametrize("week", ["2026-48", "2026-W5", "nonsense", "2027-W53", "2026-W00"])
def test_bad_weeks_are_validation_errors(api: Api, week: str) -> None:
    response = api.utilization(week=week)
    assert (response.status_code, response.json()["code"]) == (422, "validation_error")


def test_week_is_required(api: Api) -> None:
    response = api.utilization()
    assert (response.status_code, response.json()["code"]) == (422, "validation_error")


def test_unknown_venue_is_404(api: Api) -> None:
    response = api.utilization(week="2026-W48", venue_id=str(uuid.uuid4()))
    assert (response.status_code, response.json()["code"]) == (404, "venue_not_found")

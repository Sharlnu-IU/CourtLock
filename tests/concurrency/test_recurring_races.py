"""T-R5: recurring bookings under concurrency. Never a double-booking, never a partial series."""

import random
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import Engine

from tests.support.api import Api
from tests.support.factories import (
    count_rows,
    make_court,
    make_members,
    make_venue,
    overlapping_active_pairs,
    series_integrity_violations,
)
from tests.support.times import next_weekday

WEEKS = 12


def test_20_members_racing_for_the_same_weekly_slot_one_wins_cleanly(
    apis: list[Api], engine: Engine, court: uuid.UUID
) -> None:
    members = make_members(engine, 20)
    first = next_weekday(1, 10)

    def attempt(index: int) -> tuple[int, str]:
        response = apis[index % 2].series(members[index], court, first, WEEKS)
        return response.status_code, response.json().get("code", "")

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(attempt, range(20)))

    assert Counter(code for code, _ in results) == {201: 1, 409: 19}
    assert {name for code, name in results if code == 409} == {"series_conflict"}
    assert count_rows(engine, "SELECT count(*) FROM recurring_series") == 1
    assert count_rows(engine, "SELECT count(*) FROM bookings") == WEEKS  # exactly one full series
    assert count_rows(engine, "SELECT count(*) FROM ledger_entries") == WEEKS
    assert series_integrity_violations(engine) == 0


def test_a_series_racing_single_holds_on_one_of_its_weeks_has_one_winner(
    apis: list[Api], engine: Engine, court: uuid.UUID
) -> None:
    members = make_members(engine, 20)
    first = next_weekday(1, 10)
    week_three = first + timedelta(weeks=2)

    def attempt(index: int) -> tuple[str, int]:
        api = apis[index % 2]
        if index < 10:
            return "series", api.series(members[index], court, first, 4).status_code
        return "hold", api.hold(members[index], court, week_three).status_code

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(attempt, range(20)))

    assert all(code in (201, 409) for _, code in results)  # never a 500 (e.g. a deadlock)
    series_won = sum(1 for kind, code in results if kind == "series" and code == 201)
    holds_won = sum(1 for kind, code in results if kind == "hold" and code == 201)
    assert (series_won, holds_won) in {(1, 0), (0, 1)}  # week 3 has exactly one owner
    assert overlapping_active_pairs(engine) == 0
    assert series_integrity_violations(engine) == 0


def test_random_overlapping_series_never_overlap_deadlock_or_split(
    apis: list[Api], engine: Engine
) -> None:
    courts = [make_court(engine, make_venue(engine)) for _ in range(2)]
    members = make_members(engine, 40)
    base = next_weekday(1, 10)
    rng = random.Random(7)
    requests = [
        (
            rng.choice(courts),
            base + timedelta(weeks=rng.randint(0, 3), hours=rng.randint(0, 1)),
            rng.randint(2, 6),
        )
        for _ in members
    ]

    def attempt(index: int) -> int:
        court, first, weeks = requests[index]
        return apis[index % 2].series(members[index], court, first, weeks).status_code

    with ThreadPoolExecutor(max_workers=40) as pool:
        codes = list(pool.map(attempt, range(len(members))))

    assert set(codes) <= {201, 409}, Counter(codes)  # no 500s: deadlocks are retried, not leaked
    assert 201 in codes
    assert overlapping_active_pairs(engine) == 0
    assert series_integrity_violations(engine) == 0
    assert count_rows(engine, "SELECT count(*) FROM recurring_series") == codes.count(201)


def test_one_member_racing_series_and_holds_cannot_exceed_the_cap(
    apis: list[Api], engine: Engine, court: uuid.UUID, member: uuid.UUID
) -> None:
    base = next_weekday(1, 10)

    def attempt(index: int) -> int:
        api = apis[index % 2]
        if index < 5:  # five different series on different days
            return api.series(member, court, base + timedelta(days=index), 3).status_code
        return api.hold(member, court, base + timedelta(days=index, hours=2)).status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        codes = list(pool.map(attempt, range(10)))

    assert Counter(codes) == {201: 3, 409: 7}  # three commitments, series and holds alike
    assert series_integrity_violations(engine) == 0

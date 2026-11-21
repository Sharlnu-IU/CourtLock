"""BR-11: full refund with at least 24 hours' notice, otherwise none."""

from datetime import UTC, datetime, timedelta

import pytest

from courtlock.domain.refund import refund_amount, refund_percent

START = datetime(2026, 12, 1, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("notice", "expected"),
    [
        (timedelta(days=7), 100),
        (timedelta(hours=24, seconds=1), 100),
        (timedelta(hours=24), 100),  # exactly 24 h counts as "at least 24 h"
        (timedelta(hours=24) - timedelta(microseconds=1), 0),
        (timedelta(hours=23, minutes=59, seconds=59), 0),
        (timedelta(hours=1), 0),
        (timedelta(0), 0),
    ],
)
def test_refund_percent_boundary(notice: timedelta, expected: int) -> None:
    assert refund_percent(START, START - notice) == expected


@pytest.mark.parametrize(
    ("price", "percent", "expected"),
    [(1200, 100, 1200), (1200, 0, 0), (0, 100, 0), (999, 100, 999)],
)
def test_refund_amount(price: int, percent: int, expected: int) -> None:
    assert refund_amount(price, percent) == expected

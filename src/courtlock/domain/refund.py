"""Cancellation refund policy (BR-11): full refund with at least 24 hours' notice, else none."""

from datetime import datetime, timedelta

FULL_REFUND_NOTICE = timedelta(hours=24)


def refund_percent(start_at: datetime, now: datetime) -> int:
    """100 if the slot starts at least 24 h from `now` (exactly 24 h counts), else 0."""
    return 100 if start_at - now >= FULL_REFUND_NOTICE else 0


def refund_amount(price_cents: int, percent: int) -> int:
    return price_cents * percent // 100

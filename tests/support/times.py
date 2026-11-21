"""Time helpers. Business time always comes from the database clock (BR-22)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, text


def at(days_ahead: int, hour: int = 10) -> datetime:
    """UTC start of the slot `days_ahead` days from today at `hour`:00."""
    day = (datetime.now(UTC) + timedelta(days=days_ahead)).date()
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def next_weekday(weekday: int, hour: int, min_days_ahead: int = 2) -> datetime:
    """The first UTC slot at `hour`:00 on `weekday` (0 = Monday) at least N days from today."""
    candidate = at(min_days_ahead, hour)
    while candidate.weekday() != weekday:
        candidate += timedelta(days=1)
    return candidate


def iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def db_now(engine: Engine) -> datetime:
    with engine.connect() as conn:
        value: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
    return value.astimezone(UTC)

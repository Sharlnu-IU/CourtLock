"""Failure injection with temporary database triggers (D-17).

Production code contains no test hooks. To make a step fail we install a trigger that raises
(or sleeps) on a chosen statement, call the endpoint, and drop the trigger afterwards.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, text


@contextmanager
def fault_trigger(
    engine: Engine,
    *,
    table: str,
    event: str,
    when: str | None = None,
    sleep_seconds: float | None = None,
) -> Iterator[None]:
    """While active, every matching `event` on `table` raises (or sleeps, then proceeds).

    `when` is an optional SQL condition on NEW, e.g. "NEW.status = 'CANCELLED'".
    Named zz_* so it fires after the application's own triggers.
    """
    body = (
        f"PERFORM pg_sleep({sleep_seconds}); RETURN NEW;"
        if sleep_seconds is not None
        else "RAISE EXCEPTION 'injected failure';"
    )
    when_clause = f"WHEN ({when})" if when else ""
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE OR REPLACE FUNCTION zz_test_fault() RETURNS trigger LANGUAGE plpgsql AS $$"
                f" BEGIN {body} END $$"
            )
        )
        conn.execute(
            text(
                f"CREATE TRIGGER zz_test_fault BEFORE {event} ON {table}"
                f" FOR EACH ROW {when_clause} EXECUTE FUNCTION zz_test_fault()"
            )
        )
    try:
        yield
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TRIGGER IF EXISTS zz_test_fault ON {table}"))
            conn.execute(text("DROP FUNCTION IF EXISTS zz_test_fault()"))

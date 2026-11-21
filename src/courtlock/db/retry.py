"""Re-run a whole use-case when Postgres aborts it for a deadlock or serialization failure.

Safe because every use-case is exactly one transaction: an aborted attempt left nothing behind.
"""

import random
import time
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy.exc import OperationalError

RETRYABLE_SQLSTATES = frozenset({"40P01", "40001"})
MAX_ATTEMPTS = 3

T = TypeVar("T")


def run_with_retry(use_case: Callable[[], T]) -> T:
    attempt = 1
    while True:
        try:
            return use_case()
        except OperationalError as exc:
            sqlstate = getattr(exc.orig, "sqlstate", None)
            if sqlstate not in RETRYABLE_SQLSTATES or attempt >= MAX_ATTEMPTS:
                raise
            time.sleep(random.uniform(0.01, 0.05) * attempt)
            attempt += 1

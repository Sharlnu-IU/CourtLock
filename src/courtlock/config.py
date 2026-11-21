"""Runtime settings, read from environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Policy numbers and connection details. Names map to upper-case env vars."""

    database_url: str = "postgresql+psycopg://courtlock:courtlock@127.0.0.1:5432/courtlock"
    staff_api_key: str = "dev-staff-key"
    hold_ttl_seconds: int = 300
    max_active_bookings: int = 3
    booking_horizon_days: int = 90
    db_pool_size: int = 10
    db_max_overflow: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()

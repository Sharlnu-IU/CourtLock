"""Shared fixtures: a real Postgres, migrated by Alembic, and real HTTP servers."""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from courtlock.config import Settings
from courtlock.db.session import make_engine
from tests.support.api import Api
from tests.support.factories import TABLES, make_court, make_member, make_venue
from tests.support.servers import LiveServer

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "postgresql+psycopg://courtlock:courtlock@127.0.0.1:5432/courtlock_test"


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_URL)


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    """A freshly created test database, built by running the real Alembic migration."""
    url = make_url(database_url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False))
    command.upgrade(config, "head")

    test_engine = make_engine(Settings(database_url=database_url, db_pool_size=20))
    yield test_engine
    test_engine.dispose()


def _settings(database_url: str, hold_ttl_seconds: int = 300) -> Settings:
    return Settings(
        database_url=database_url,
        staff_api_key="test-staff-key",
        hold_ttl_seconds=hold_ttl_seconds,
    )


@pytest.fixture(scope="session")
def servers(database_url: str, engine: Engine) -> Iterator[list[LiveServer]]:
    """Two independent API instances against the same database."""
    instances = [LiveServer(_settings(database_url)) for _ in range(2)]
    for instance in instances:
        instance.start()
    yield instances
    for instance in instances:
        instance.stop()


@pytest.fixture(scope="session")
def short_ttl_server(database_url: str, engine: Engine) -> Iterator[LiveServer]:
    """An instance whose holds last one second, for one real-time expiry test."""
    server = LiveServer(_settings(database_url, hold_ttl_seconds=1))  # one-second holds
    server.start()
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def _clean_database(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(reversed(TABLES))} CASCADE"))


@pytest.fixture
def api(servers: list[LiveServer]) -> Iterator[Api]:
    client = Api(servers[0].base_url)
    yield client
    client.close()


@pytest.fixture
def apis(servers: list[LiveServer]) -> Iterator[list[Api]]:
    clients = [Api(server.base_url) for server in servers]
    yield clients
    for client in clients:
        client.close()


@pytest.fixture
def court(engine: Engine) -> uuid.UUID:
    """A UTC venue open 06:00-22:00 (peak Mon-Fri 17:00-21:00) with one court."""
    return make_court(engine, make_venue(engine))


@pytest.fixture
def member(engine: Engine) -> uuid.UUID:
    return make_member(engine)


@pytest.fixture
def other_member(engine: Engine) -> uuid.UUID:
    return make_member(engine)

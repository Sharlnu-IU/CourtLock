"""Alembic environment: plain SQL migrations, URL from config or DATABASE_URL."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from courtlock.config import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _url() -> str:
    configured = config.get_main_option("sqlalchemy.url")
    return configured if configured else get_settings().database_url


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations_online()

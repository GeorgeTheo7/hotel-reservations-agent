"""Alembic environment. Migrations are plain SQL (op.execute); no SQLAlchemy models.

SQLAlchemy is used only as Alembic's connection wrapper: the engine gets its connections
from our own `connect()` (psycopg 3), so the same DATABASE_URL works everywhere.
"""

import logging

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from anemos.db.connection import connect, database_url

config = context.config

# CLI runs: show "Running upgrade ..." lines. Leave logging alone when a host
# (pytest, the app) has already configured it.
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(message)s")


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql`: print the SQL instead of running it."""
    context.configure(dialect_name="postgresql", literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    conninfo: str = config.attributes.get("conninfo") or database_url()
    engine = create_engine(
        "postgresql+psycopg://", creator=lambda: connect(conninfo), poolclass=NullPool
    )
    with engine.connect() as connection:
        context.configure(connection=connection)
        # Postgres DDL is transactional: all pending migrations commit together or not at all.
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

"""Run Alembic migrations from code (tests, later the app). The CLI uses [tool.alembic] in
pyproject.toml instead: uv run --env-file .env alembic upgrade head
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(conninfo: str | None = None) -> Config:
    """Config for the database at `conninfo` (URL or key=value), default DATABASE_URL."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # Passed to env.py as an attribute, not as an ini option, so passwords containing
    # '%' are not mangled by configparser interpolation.
    config.attributes["conninfo"] = conninfo
    return config


def upgrade(conninfo: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(conninfo), revision)


def downgrade(conninfo: str | None = None, revision: str = "base") -> None:
    command.downgrade(alembic_config(conninfo), revision)


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()

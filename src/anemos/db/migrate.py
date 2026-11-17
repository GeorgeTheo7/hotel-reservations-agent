"""Our migrate command: Alembic migrations, then the LangGraph checkpoint tables.

    uv run --env-file .env python -m anemos.db.migrate

`upgrade` to head also runs `PostgresSaver.setup()` (anemos.checkpoints, D75), which
creates or upgrades the library's own tables in the `langgraph` schema; they are not
copied into an Alembic migration, which would drift on library upgrades. The plain
`alembic upgrade head` CLI (via [tool.alembic] in pyproject.toml) still works for the
application schema, and `--sql` prints a migration's SQL, but it does not set up the
checkpoint tables.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from anemos.checkpoints import setup_checkpoints

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
    if revision == "head":
        setup_checkpoints(conninfo)


def downgrade(conninfo: str | None = None, revision: str = "base") -> None:
    command.downgrade(alembic_config(conninfo), revision)


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def main() -> None:
    upgrade()


if __name__ == "__main__":
    main()

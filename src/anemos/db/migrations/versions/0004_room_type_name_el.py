"""Greek room type names: room_types.name_el, for EL replies.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-13
"""

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE room_types ADD COLUMN name_el text;
        -- Existing rows get the English name as a placeholder so NOT NULL can be set;
        -- the seed writes the real Greek names.
        UPDATE room_types SET name_el = name;
        ALTER TABLE room_types ALTER COLUMN name_el SET NOT NULL;
        ALTER TABLE room_types ADD CONSTRAINT room_types_name_el_key UNIQUE (name_el);
        ALTER TABLE room_types
            ADD CONSTRAINT room_types_name_el_not_blank CHECK (btrim(name_el) <> '');
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE room_types DROP COLUMN name_el;")

"""Not-blank checks refuse tabs and newlines too (replace btrim with ~ '\\S').

`btrim(x) <> ''` trims spaces only, so a value of tabs or newlines passed (docs/NOTES.md).
Same constraint names, stricter predicate. Adding a CHECK validates existing rows, so
this migration fails loudly if a database already holds such a value.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-13
"""

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        -- \\S = any non-whitespace character.
        ALTER TABLE room_types
            DROP CONSTRAINT room_types_name_el_not_blank,
            ADD CONSTRAINT room_types_name_el_not_blank CHECK (name_el ~ '\\S');

        ALTER TABLE emails
            DROP CONSTRAINT emails_message_id_not_blank,
            ADD CONSTRAINT emails_message_id_not_blank CHECK (message_id ~ '\\S'),
            DROP CONSTRAINT emails_from_address_not_blank,
            ADD CONSTRAINT emails_from_address_not_blank CHECK (from_address ~ '\\S');
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE emails
            DROP CONSTRAINT emails_from_address_not_blank,
            ADD CONSTRAINT emails_from_address_not_blank CHECK (btrim(from_address) <> ''),
            DROP CONSTRAINT emails_message_id_not_blank,
            ADD CONSTRAINT emails_message_id_not_blank CHECK (btrim(message_id) <> '');

        ALTER TABLE room_types
            DROP CONSTRAINT room_types_name_el_not_blank,
            ADD CONSTRAINT room_types_name_el_not_blank CHECK (btrim(name_el) <> '');
        """
    )

"""draft_offers.rendered_block: the quote block exactly as the agent's version showed it.

An owner edit passes the price check only if every rendered block still appears
verbatim (D63). Stored at draft creation, write-once with the rest of draft_offers
(D51), so a later change to templates or room names cannot make the check misfire.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-14
"""

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # NOT NULL without a default: fails loudly on a database that already has offers
    # (none exist outside tests; inventing blocks for old offers would defeat D63).
    op.execute(
        """
        ALTER TABLE draft_offers
            ADD COLUMN rendered_block text NOT NULL,
            ADD CONSTRAINT draft_offers_rendered_block_not_blank
                CHECK (rendered_block ~ '\\S');
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE draft_offers DROP COLUMN rendered_block;")

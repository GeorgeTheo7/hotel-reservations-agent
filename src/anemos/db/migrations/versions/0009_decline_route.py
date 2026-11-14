"""Drafts may take the `decline` route (D62).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-14
"""

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE drafts
            DROP CONSTRAINT drafts_route_valid,
            ADD CONSTRAINT drafts_route_valid
                CHECK (route IN ('quote', 'ask_info', 'policy', 'decline'));
        """
    )


def downgrade() -> None:
    # Fails if a decline draft exists: the downgrade must not silently relabel one.
    op.execute(
        """
        ALTER TABLE drafts
            DROP CONSTRAINT drafts_route_valid,
            ADD CONSTRAINT drafts_route_valid CHECK (route IN ('quote', 'ask_info', 'policy'));
        """
    )

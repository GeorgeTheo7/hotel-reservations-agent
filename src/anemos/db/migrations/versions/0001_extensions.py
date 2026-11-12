"""Extensions: btree_gist (exclusion constraints on bookings) and vector (policy embeddings).

Revision ID: 0001
Revises:
Create Date: 2026-11-12
"""

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE EXTENSION IF NOT EXISTS btree_gist;
        CREATE EXTENSION IF NOT EXISTS vector;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP EXTENSION IF EXISTS vector;
        DROP EXTENSION IF EXISTS btree_gist;
        """
    )

"""Indexes for the owner's inbox (GET /emails): newest first, keyset (received_at, id).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-15
"""

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        -- The inbox filtered by one status: seek to the status, read newest first, and
        -- resume after the cursor (received_at, id).
        CREATE INDEX emails_status_received_at_id_idx
            ON emails (status, received_at DESC, id DESC);

        -- The default inbox (every status except set_aside, D50). Partial, so set-aside
        -- rows are not even in it; the query must repeat `status <> 'set_aside'`
        -- exactly (api/reads.py inbox_query) for the planner to use it.
        CREATE INDEX emails_visible_received_at_id_idx
            ON emails (received_at DESC, id DESC)
            WHERE status <> 'set_aside';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX emails_visible_received_at_id_idx;
        DROP INDEX emails_status_received_at_id_idx;
        """
    )

"""outbox.next_attempt_at: when a pending message may be tried again (step 15).

NULL = due now (never tried). After failed attempt n the sender sets it to
now + 2^(n-1) minutes, capped at 60. No row is updated here: sent rows are frozen by
the D51 trigger, and NULL needs no value. All times come from the injected clock (D20).

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-15
"""

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE outbox
            ADD COLUMN next_attempt_at timestamptz,
            ADD CONSTRAINT outbox_next_attempt_not_before_created
                CHECK (next_attempt_at >= created_at);

        -- The sender's claim query filters and sorts on exactly this expression, so the
        -- planner can use the index (an index on an expression only serves queries that
        -- repeat the same expression).
        CREATE INDEX outbox_pending_due_idx
            ON outbox (coalesce(next_attempt_at, created_at), id)
            WHERE status = 'pending';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX outbox_pending_due_idx;
        ALTER TABLE outbox DROP COLUMN next_attempt_at;
        """
    )

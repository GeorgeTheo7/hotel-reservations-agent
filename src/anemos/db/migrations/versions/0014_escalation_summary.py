"""emails.escalation_summary: the model's 2-3 line English summary of an escalated email
for the owner (D81). NULL when there is none (INJECTION, THREAD_REPLY, a failed or
refused summary call, an extraction that failed, the stub path).

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-17
"""

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE emails ADD COLUMN escalation_summary text")


def downgrade() -> None:
    op.execute("ALTER TABLE emails DROP COLUMN escalation_summary")

"""owner_sessions: one row per logged-in browser (D82). Only the SHA-256 of the session
token is stored, never the token, so a copy of this table cannot be used to log in.
Rows are deleted at logout; an expired row is ignored by every check (tidied in week
10). Not write-once and not under D51's triggers.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-18
"""

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE owner_sessions (
            token_sha256 bytea PRIMARY KEY CHECK (length(token_sha256) = 32),
            created_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            CHECK (expires_at > created_at)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE owner_sessions")

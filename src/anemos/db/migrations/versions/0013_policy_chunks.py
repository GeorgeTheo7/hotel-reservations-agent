"""policy_chunks: the hotel's policies with their embeddings and full-text vectors (D15).

Seed data, not operational: replaced as a whole when the policies are loaded
(anemos.policies.store), so not write-once (unlike D51's tables). No vector index:
about 25 rows are searched exactly, so recall is exact (D15).

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-16
"""

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE policy_chunks (
            policy_id       text PRIMARY KEY,
            title           text NOT NULL,
            text            text NOT NULL,
            text_sha256     text NOT NULL,      -- of the text embedded (title + text)
            embedding       vector(1536) NOT NULL,
            embedding_model text NOT NULL,      -- a query from another model is refused
            -- Full-text side of the hybrid search; generated, so it cannot drift.
            tsv             tsvector NOT NULL
                GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || text)) STORED,
            CONSTRAINT policy_chunks_policy_id_slug
                CHECK (policy_id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
            CONSTRAINT policy_chunks_title_not_blank CHECK (title ~ '\\S'),
            CONSTRAINT policy_chunks_text_not_blank CHECK (text ~ '\\S'),
            CONSTRAINT policy_chunks_embedding_model_not_blank CHECK (embedding_model ~ '\\S'),
            CONSTRAINT policy_chunks_text_sha256_hex CHECK (text_sha256 ~ '^[0-9a-f]{64}$')
        );

        CREATE INDEX policy_chunks_tsv_idx ON policy_chunks USING gin (tsv);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE policy_chunks;")

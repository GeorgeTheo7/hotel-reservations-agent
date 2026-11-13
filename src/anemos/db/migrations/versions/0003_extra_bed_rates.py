"""Extra-bed rates: price per extra bed per night, by age band.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-13
"""

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        -- Who pays an extra bed is decided in code (D25); this table says how much.
        -- Infants under 2 sleep in a free cot (D13): no row, no charge.
        -- The age bounds of each band (2-11, 12+) live in code with the band names,
        -- like the season ids: the CHECK below keeps the two in step.
        -- One price for all seasons and room types, as in the spec's domain data.
        CREATE TABLE extra_bed_rates (
            band       text PRIMARY KEY,
            rate_cents integer NOT NULL,                    -- per extra bed per night
            CONSTRAINT extra_bed_rates_band_valid
                CHECK (band IN ('child_2_11', 'age_12_plus')),
            CONSTRAINT extra_bed_rates_rate_positive CHECK (rate_cents > 0)
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE extra_bed_rates;")

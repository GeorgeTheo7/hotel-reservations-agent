"""Inventory: room types, rooms, seasons, season periods, room rates, minimum stays, bookings.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-12
"""

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        -- Date ranges are Postgres `daterange`, always stored half-open [start, end):
        -- for nights, `end` is the day after the last night (= check-out day).

        CREATE TABLE room_types (
            id                   text PRIMARY KEY,          -- e.g. 'sea_view'
            name                 text NOT NULL UNIQUE,      -- e.g. 'Sea View Double'
            standard_occupancy   smallint NOT NULL,
            max_occupancy        smallint NOT NULL,
            -- Physical extra sleeping place, for wording. Pricing rule (week 2): every
            -- person above standard_occupancy pays the extra-bed rate.
            extra_beds           text NOT NULL,
            -- Accessible room: offered without a stated need only if nothing else fits.
            offer_only_if_needed boolean NOT NULL DEFAULT false,
            CONSTRAINT room_types_occupancy_valid
                CHECK (standard_occupancy >= 1 AND standard_occupancy <= max_occupancy),
            CONSTRAINT room_types_extra_beds_valid
                CHECK (extra_beds IN ('cot_only', 'extra_bed', 'sofa_beds'))
        );

        CREATE TABLE rooms (
            id           smallint PRIMARY KEY,              -- the room number, e.g. 201
            room_type_id text NOT NULL REFERENCES room_types (id),
            CONSTRAINT rooms_number_positive CHECK (id > 0)
        );

        -- What a season is: one row per season, so city tax is stored once.
        CREATE TABLE seasons (
            id             text PRIMARY KEY,
            city_tax_cents integer NOT NULL,                -- per room-night
            CONSTRAINT seasons_id_valid CHECK (id IN ('low', 'mid', 'high')),
            CONSTRAINT seasons_city_tax_non_negative CHECK (city_tax_cents >= 0)
        );

        -- When a season is: one row per date range (Low and Mid have two each).
        -- The only calendar of the hotel: a night in no period = hotel closed.
        CREATE TABLE season_periods (
            id        integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            season_id text NOT NULL REFERENCES seasons (id),
            nights    daterange NOT NULL,
            CONSTRAINT season_periods_nights_bounded
                CHECK (NOT isempty(nights) AND NOT lower_inf(nights) AND NOT upper_inf(nights)),
            CONSTRAINT season_periods_no_overlap EXCLUDE USING gist (nights WITH &&)
        );

        -- Rates have no dates of their own: a night's rate = its season's rate, so rates
        -- and the calendar cannot disagree. Prices per year: spec "Later".
        CREATE TABLE room_rates (
            room_type_id text NOT NULL REFERENCES room_types (id),
            season_id    text NOT NULL REFERENCES seasons (id),
            rate_cents   integer NOT NULL,                  -- per room-night
            PRIMARY KEY (room_type_id, season_id),
            CONSTRAINT room_rates_rate_positive CHECK (rate_cents > 0)
        );

        -- Minimum stay depends on the arrival date only, not on the room type.
        -- Dated on purpose: the 10-20 Aug rule cuts across High season.
        CREATE TABLE min_stay_rules (
            id         integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            arrivals   daterange NOT NULL,
            min_nights smallint NOT NULL,
            CONSTRAINT min_stay_rules_arrivals_bounded
                CHECK (NOT isempty(arrivals)
                       AND NOT lower_inf(arrivals) AND NOT upper_inf(arrivals)),
            CONSTRAINT min_stay_rules_min_nights_positive CHECK (min_nights >= 1),
            CONSTRAINT min_stay_rules_no_overlap EXCLUDE USING gist (arrivals WITH &&)
        );

        CREATE TABLE bookings (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            room_id     smallint NOT NULL REFERENCES rooms (id),
            stay        daterange NOT NULL,                 -- [check-in, check-out)
            status      text NOT NULL,
            expires_at  timestamptz,                        -- holds only (48 h, D9)
            request_id  bigint,                             -- FK to requests added in week 3
            email_id    bigint,                             -- FK to emails added in week 3
            price_cents integer NOT NULL,
            -- No DEFAULT now(): the code passes the injected clock's time (D20).
            created_at  timestamptz NOT NULL,
            CONSTRAINT bookings_status_valid
                CHECK (status IN ('hold', 'confirmed', 'cancelled', 'expired')),
            CONSTRAINT bookings_stay_bounded
                CHECK (NOT isempty(stay) AND NOT lower_inf(stay) AND NOT upper_inf(stay)),
            CONSTRAINT bookings_hold_has_expiry
                CHECK (status <> 'hold' OR expires_at IS NOT NULL),
            CONSTRAINT bookings_price_non_negative CHECK (price_cents >= 0),
            -- The core safety rule: one room cannot have two active stays that overlap.
            -- Cancelled/expired rows are ignored. A hold past expires_at still blocks here
            -- until approval flips it to 'expired' (D10, D22).
            CONSTRAINT bookings_no_double_booking
                EXCLUDE USING gist (room_id WITH =, stay WITH &&)
                WHERE (status IN ('hold', 'confirmed'))
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE bookings;
        DROP TABLE min_stay_rules;
        DROP TABLE room_rates;
        DROP TABLE season_periods;
        DROP TABLE seasons;
        DROP TABLE rooms;
        DROP TABLE room_types;
        """
    )

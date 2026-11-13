"""Quotes, drafts, draft text versions and draft offers.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-13
"""

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        -- One priced option, stored by quote_price (spec Tools). Mirrors pricing.Quote;
        -- the party is the request's. No inventory effect: holds are bookings.
        CREATE TABLE quotes (
            id                           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            request_id                   bigint NOT NULL REFERENCES requests (id),
            room_type_id                 text NOT NULL REFERENCES room_types (id),
            stay                         daterange NOT NULL,     -- [check-in, check-out)
            rooms                        smallint NOT NULL,
            lines                        jsonb NOT NULL,         -- one NightLine per night
            extra_bed_bands              text[] NOT NULL,        -- one per paying guest
            room_subtotal_cents          integer NOT NULL,
            extra_bed_subtotal_cents     integer NOT NULL,
            accommodation_subtotal_cents integer NOT NULL,       -- D24
            city_tax_cents               integer NOT NULL,
            deposit_cents                integer NOT NULL,       -- D14
            free_cancellation_until      date NOT NULL,          -- D26
            -- How this quote differs from its request; empty = exactly what was asked.
            -- A set, because changes combine (open part + min-stay version, D34).
            changes                      text[] NOT NULL,
            -- D9, the quote's half: it matches the request exactly. Derived from
            -- `changes`, so the two can never disagree. (The draft's half, "it is the
            -- only offer", is guardrail 7 in code: a CHECK cannot count other rows.)
            hold_allowed                 boolean NOT NULL
                GENERATED ALWAYS AS (cardinality(changes) = 0) STORED,
            created_at                   timestamptz NOT NULL,   -- injected clock (D20)
            CONSTRAINT quotes_stay_bounded
                CHECK (NOT isempty(stay) AND NOT lower_inf(stay) AND NOT upper_inf(stay)),
            -- D5: at most 2 rooms; 3+ is GROUP and never priced.
            CONSTRAINT quotes_rooms_valid CHECK (rooms BETWEEN 1 AND 2),
            -- One line per night: a quote that lost or doubled a night is refused.
            CONSTRAINT quotes_one_line_per_night
                CHECK (jsonb_typeof(lines) = 'array'
                       AND jsonb_array_length(lines) = upper(stay) - lower(stay)),
            -- open_part: closed nights cut off (D34) · min_stay: lengthened to the
            -- minimum (D7) · other_type: a room type other than the one asked (unmet
            -- request, over capacity D27) · more_rooms: 2 rooms instead of 1 (D27) ·
            -- other_dates: a same-length range within +-7 d (all booked) ·
            -- window_option: one of the options for approximate dates (D7, D28).
            CONSTRAINT quotes_changes_valid
                CHECK (changes <@ ARRAY['open_part', 'min_stay', 'other_type', 'more_rooms',
                                        'other_dates', 'window_option']::text[]),
            CONSTRAINT quotes_extra_bed_bands_valid
                CHECK (extra_bed_bands <@ ARRAY['child_2_11', 'age_12_plus']::text[]),
            CONSTRAINT quotes_amounts_valid
                CHECK (room_subtotal_cents > 0 AND extra_bed_subtotal_cents >= 0
                       AND city_tax_cents >= 0 AND deposit_cents > 0),
            -- D24 by definition; the deposit formula stays in code only (D14).
            CONSTRAINT quotes_accommodation_is_rooms_plus_extra_beds
                CHECK (accommodation_subtotal_cents
                       = room_subtotal_cents + extra_bed_subtotal_cents),
            -- Target of the composite FK from draft_offers.
            CONSTRAINT quotes_id_request_id_hold_allowed_key
                UNIQUE (id, request_id, hold_allowed)
        );

        -- At most one draft per email (states.md). Statuses: docs/states.md, Draft.
        CREATE TABLE drafts (
            id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            email_id          bigint NOT NULL REFERENCES emails (id),
            request_id        bigint,                    -- NULL: no request (policy answer)
            route             text NOT NULL,             -- guardrail 1 needs it (D53)
            status            text NOT NULL,
            created_at        timestamptz NOT NULL,      -- 72 h expiry counts from here
            decided_at        timestamptz,               -- left pending_approval at
            CONSTRAINT drafts_email_id_key UNIQUE (email_id),
            CONSTRAINT drafts_route_valid CHECK (route IN ('quote', 'ask_info', 'policy')),
            CONSTRAINT drafts_status_valid CHECK (status IN (
                'pending_approval', 'approved', 'rejected', 'expired', 'superseded',
                'invalidated')),
            -- Decided exactly when no longer pending; a born-superseded draft is decided
            -- at creation.
            CONSTRAINT drafts_decided_iff_not_pending
                CHECK ((status = 'pending_approval') = (decided_at IS NULL)),
            CONSTRAINT drafts_decided_not_before_created CHECK (decided_at >= created_at),
            -- The draft's request is the request of its own email. MATCH SIMPLE (default):
            -- a draft without a request is not checked.
            CONSTRAINT drafts_request_of_email_fkey
                FOREIGN KEY (request_id, email_id) REFERENCES requests (id, email_id),
            -- Targets of composite FKs: draft_offers (here), outbox and bookings (0007).
            CONSTRAINT drafts_id_request_id_key UNIQUE (id, request_id),
            CONSTRAINT drafts_id_status_key UNIQUE (id, status)
        );

        -- D41: every text version, with its author and the guardrail results for that
        -- text. Version 1 is the agent's draft; every later version is an owner edit.
        -- Write-once (D51, trigger in 0007).
        CREATE TABLE draft_versions (
            draft_id          bigint NOT NULL REFERENCES drafts (id),
            version           smallint NOT NULL,
            author            text NOT NULL,
            body              text NOT NULL,
            guardrail_results jsonb NOT NULL,
            created_at        timestamptz NOT NULL,
            PRIMARY KEY (draft_id, version),
            CONSTRAINT draft_versions_version_positive CHECK (version >= 1),
            CONSTRAINT draft_versions_author_valid CHECK (author IN ('agent', 'owner')),
            CONSTRAINT draft_versions_agent_writes_version_1
                CHECK ((version = 1) = (author = 'agent')),
            -- \\S = any non-whitespace character. Not btrim(body) <> '': btrim trims
            -- spaces only, so a body of newlines would pass.
            CONSTRAINT draft_versions_body_not_blank CHECK (body ~ '\\S'),
            CONSTRAINT draft_versions_guardrail_results_object
                CHECK (jsonb_typeof(guardrail_results) = 'object')
        );

        -- What the draft offers, and which offer is held at approval. Replaces the
        -- spec's quote_ids / proposed_holds arrays so every link is a foreign key.
        CREATE TABLE draft_offers (
            draft_id           bigint NOT NULL,
            request_id         bigint NOT NULL,
            quote_id           bigint NOT NULL,
            quote_hold_allowed boolean NOT NULL,      -- copy of quotes.hold_allowed
            hold               boolean NOT NULL,
            PRIMARY KEY (draft_id, quote_id),
            -- Guardrail 7, part 1: the offer's draft and quote share one request, so a
            -- draft can only offer quotes of its own run.
            CONSTRAINT draft_offers_draft_fkey
                FOREIGN KEY (draft_id, request_id) REFERENCES drafts (id, request_id),
            -- ... and the copied hold_allowed must be the quote's real value.
            CONSTRAINT draft_offers_quote_fkey
                FOREIGN KEY (quote_id, request_id, quote_hold_allowed)
                REFERENCES quotes (id, request_id, hold_allowed),
            -- Guardrail 7, part 2 / D9 / cross-rule 4: hold only what may be held.
            CONSTRAINT draft_offers_hold_only_if_allowed CHECK (NOT hold OR quote_hold_allowed)
        );

        -- D9: only the quote that matches the request exactly is held, so at most one
        -- held offer per draft (a partial unique index: unique among rows WHERE hold).
        CREATE UNIQUE INDEX draft_offers_one_hold_per_draft
            ON draft_offers (draft_id) WHERE hold;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE draft_offers;
        DROP TABLE draft_versions;
        DROP TABLE drafts;
        DROP TABLE quotes;
        """
    )

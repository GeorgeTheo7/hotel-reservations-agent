"""Emails and extracted requests; foreign keys for bookings.email_id and request_id.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-13
"""

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        -- One row per stored guest email (D4). Statuses and transitions: docs/states.md.
        CREATE TABLE emails (
            id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            message_id   text NOT NULL,
            in_reply_to  text,                              -- set = thread reply (escalated)
            -- The mailbox's thread, from the adapter. Not D17's thread_id (= the agent
            -- run, which is the email id).
            mail_thread_id text,
            from_address text NOT NULL,                     -- D48: "same guest" key
            subject      text NOT NULL,                     -- '' when the email has none
            raw_body     text NOT NULL,
            clean_body   text NOT NULL,
            language     text,                              -- NULL until extraction (week 4)
            received_at  timestamptz NOT NULL,              -- from the mailbox, UTC (D23)
            created_at   timestamptz NOT NULL,              -- stored at, injected clock (D20)
            status       text NOT NULL,
            reason_codes text[] NOT NULL DEFAULT '{}',      -- escalation reasons
            -- D4: storing the same message twice is impossible; the adapter turns the
            -- conflict into "ignored, already stored".
            CONSTRAINT emails_message_id_key UNIQUE (message_id),
            CONSTRAINT emails_message_id_not_blank CHECK (btrim(message_id) <> ''),
            CONSTRAINT emails_from_address_not_blank CHECK (btrim(from_address) <> ''),
            -- D48 compares from_address with =, so one spelling per address: the
            -- adapter lowercases it, this refuses a row where it did not.
            CONSTRAINT emails_from_address_lowercase CHECK (from_address = lower(from_address)),
            CONSTRAINT emails_language_iso CHECK (language ~ '^[a-z]{2}$'),
            CONSTRAINT emails_status_valid CHECK (status IN (
                'received', 'processing', 'set_aside', 'escalated', 'awaiting_approval',
                'answered', 'expired', 'superseded', 'closed')),
            -- Every value is a spec reason code (Escalation rules + D49). `<@` = "is
            -- contained in"; an array holding NULL is never contained, so NULL is refused too.
            CONSTRAINT emails_reason_codes_valid CHECK (reason_codes <@ ARRAY[
                'COMPLAINT', 'DISCOUNT', 'GROUP', 'CHANGE', 'CANCELLATION', 'THREAD_REPLY',
                'LANGUAGE', 'SHORT_NOTICE', 'LATE_ARRIVAL', 'NO_POLICY', 'GUARDRAIL_FAIL',
                'LOOP_LIMIT', 'LEGAL', 'BILLING', 'MEDICAL', 'INJECTION', 'MODEL',
                'ACCESSIBILITY', 'UNACCOMPANIED_CHILDREN', 'NO_CALENDAR',
                'NO_LONGER_AVAILABLE', 'OWNER_REOPENED', 'SYSTEM_ERROR']::text[]),
            -- An escalated email always says why ...
            CONSTRAINT emails_escalated_has_reason
                CHECK (status <> 'escalated' OR cardinality(reason_codes) > 0),
            -- ... and only an escalated email has reasons (kept when it is closed).
            CONSTRAINT emails_reasons_only_if_escalated
                CHECK (cardinality(reason_codes) = 0 OR status IN ('escalated', 'closed'))
        );

        -- D48: "newer" = later (received_at, id), so the lookup of a newer email from the
        -- same address is one index range scan.
        CREATE INDEX emails_from_address_received_at_id_idx
            ON emails (from_address, received_at, id);

        -- The extracted request of an email (D5: one stay per email, so one request).
        -- Fields are NULL when extraction could not fill them (escalated / NEEDS_INFO).
        CREATE TABLE requests (
            id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            email_id     bigint NOT NULL REFERENCES emails (id),
            extracted    jsonb NOT NULL,                    -- DateSpec, flags, script, as given
            stay         daterange,                         -- resolved [check-in, check-out)
            adults       smallint,
            children     smallint,
            child_ages   smallint[] NOT NULL DEFAULT '{}',  -- may be shorter: ages missing
            rooms        smallint,
            room_type_id text REFERENCES room_types (id),   -- NULL = no type named
            created_at   timestamptz NOT NULL,              -- injected clock (D20)
            CONSTRAINT requests_email_id_key UNIQUE (email_id),
            -- Target of the composite FK from bookings below.
            CONSTRAINT requests_id_email_id_key UNIQUE (id, email_id),
            CONSTRAINT requests_stay_bounded
                CHECK (NOT isempty(stay) AND NOT lower_inf(stay) AND NOT upper_inf(stay)),
            CONSTRAINT requests_adults_non_negative CHECK (adults >= 0),
            -- Kept for its error name only: it cannot fire alone, because a negative
            -- count also fails requests_child_ages_not_more_than_children (0 ages <= -1).
            CONSTRAINT requests_children_non_negative CHECK (children >= 0),
            CONSTRAINT requests_rooms_positive CHECK (rooms >= 1),
            -- Same bounds as model.Child (0-17); `<= ALL` is true for an empty array.
            CONSTRAINT requests_child_ages_valid
                CHECK (0 <= ALL (child_ages) AND 17 >= ALL (child_ages)
                       AND array_position(child_ages, NULL) IS NULL),
            CONSTRAINT requests_child_ages_not_more_than_children
                CHECK (cardinality(child_ages) <= coalesce(children, 0))
        );

        -- Promised in week 1: bookings.email_id and request_id get a foreign key. One
        -- two-column key does it: the request must be the request of that same email,
        -- and requests.email_id already points at emails. MATCH FULL: a booking has both
        -- or neither (a fixture booking has neither). Separate one-column keys would
        -- never refuse a row this one accepts, so there are none (docs/NOTES.md).
        ALTER TABLE bookings
            ADD CONSTRAINT bookings_request_of_email_fkey
                FOREIGN KEY (request_id, email_id) REFERENCES requests (id, email_id)
                MATCH FULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE bookings DROP CONSTRAINT bookings_request_of_email_fkey;
        DROP TABLE requests;
        DROP TABLE emails;
        """
    )

"""Outbox, bookings.draft_id, audit log, and the D51 triggers (final statuses, write-once).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-13
"""

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        -- Target of the bookings FK below: a booking's draft, its request, and that
        -- draft's status, checked together.
        ALTER TABLE drafts
            ADD CONSTRAINT drafts_id_request_id_status_key UNIQUE (id, request_id, status);

        -- One message per approved draft (states.md, Outbox). Sending is simulated (D4).
        CREATE TABLE outbox (
            id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            draft_id     bigint NOT NULL,
            -- Always 'approved', so the FK below means "the draft is approved".
            draft_status text NOT NULL GENERATED ALWAYS AS ('approved') STORED,
            -- The message exactly as it will be sent; never changes (D51).
            to_address   text NOT NULL,
            subject      text NOT NULL,
            body         text NOT NULL,
            status       text NOT NULL,
            attempts     integer NOT NULL DEFAULT 0,
            last_error   text,
            created_at   timestamptz NOT NULL,          -- injected clock (D20)
            sent_at      timestamptz,
            -- Cross-rule 2: exactly one outbox row per draft; a double send is impossible.
            CONSTRAINT outbox_draft_id_key UNIQUE (draft_id),
            -- Cross-rule 2: an outbox row only for an approved draft. DEFERRABLE
            -- INITIALLY DEFERRED: checked at COMMIT, so the approval transaction may
            -- write the outbox row before it sets the draft to 'approved' (D10 order).
            CONSTRAINT outbox_draft_approved_fkey
                FOREIGN KEY (draft_id, draft_status) REFERENCES drafts (id, status)
                DEFERRABLE INITIALLY DEFERRED,
            CONSTRAINT outbox_status_valid CHECK (status IN ('pending', 'sent')),
            CONSTRAINT outbox_sent_at_iff_sent CHECK ((status = 'sent') = (sent_at IS NOT NULL)),
            CONSTRAINT outbox_sent_not_before_created CHECK (sent_at >= created_at),
            CONSTRAINT outbox_attempts_valid
                CHECK (attempts >= 0 AND (status <> 'sent' OR attempts >= 1)),
            CONSTRAINT outbox_to_address_lowercase CHECK (to_address = lower(to_address)),
            CONSTRAINT outbox_to_address_not_blank CHECK (to_address ~ '\\S'),
            CONSTRAINT outbox_body_not_blank CHECK (body ~ '\\S')
        );

        -- Cross-rule 3: a booking created by the system points to the request of an
        -- approved draft. One FK over (draft, request, 'approved'): MATCH FULL means a
        -- booking has a draft and a request, or neither (test fixtures); with the 0005
        -- key, the request is also that email's. Deferred, like the outbox FK.
        ALTER TABLE bookings
            ADD COLUMN draft_id bigint,
            ADD COLUMN draft_status text
                GENERATED ALWAYS AS (CASE WHEN draft_id IS NOT NULL THEN 'approved' END) STORED,
            ADD CONSTRAINT bookings_draft_approved_fkey
                FOREIGN KEY (draft_id, request_id, draft_status)
                REFERENCES drafts (id, request_id, status)
                MATCH FULL DEFERRABLE INITIALLY DEFERRED;

        -- Append-only record of every step and owner action (spec Data model).
        CREATE TABLE audit_log (
            id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            at             timestamptz NOT NULL,        -- injected clock (D20)
            actor          text NOT NULL,
            event          text NOT NULL,               -- e.g. 'draft.approved'
            email_id       bigint REFERENCES emails (id),   -- = the run (D17: thread_id)
            draft_id       bigint REFERENCES drafts (id),
            step           text,                        -- graph node, from week 6
            input          jsonb,
            output         jsonb,
            model          text,
            prompt_version text,
            input_tokens   integer,
            output_tokens  integer,
            cost_micro_usd bigint,                      -- integer, like money (D14)
            latency_ms     integer,
            error          text,
            CONSTRAINT audit_log_actor_valid CHECK (actor IN ('owner', 'agent', 'system')),
            CONSTRAINT audit_log_event_format CHECK (event ~ '^[a-z][a-z_]*(\\.[a-z_]+)+$'),
            CONSTRAINT audit_log_usage_non_negative
                CHECK (input_tokens >= 0 AND output_tokens >= 0
                       AND cost_micro_usd >= 0 AND latency_ms >= 0)
        );

        -- The detail page reads one email's (= one run's) history.
        CREATE INDEX audit_log_email_id_idx ON audit_log (email_id);

        -- D51, part 1: a row in a final status is frozen. The final statuses of each
        -- table (docs/states.md) are passed as trigger arguments (TG_ARGV).
        -- Messages are built with ||, not RAISE placeholders: Alembic doubles percent signs.
        -- ERRCODE 23001 = restrict_violation; CONSTRAINT names the rule in the error,
        -- like a real constraint, so tests can assert which rule fired.
        CREATE FUNCTION refuse_change_to_final_row() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status = ANY (TG_ARGV) THEN
                RAISE EXCEPTION USING
                    MESSAGE = TG_TABLE_NAME || ' ' || OLD.id || ' has final status '
                              || OLD.status || ': it cannot be changed',
                    ERRCODE = 'restrict_violation',
                    CONSTRAINT = TG_TABLE_NAME || '_final_status';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$;

        CREATE TRIGGER emails_final_status BEFORE UPDATE OR DELETE ON emails
            FOR EACH ROW EXECUTE FUNCTION
            refuse_change_to_final_row('answered', 'superseded', 'closed');
        CREATE TRIGGER drafts_final_status BEFORE UPDATE OR DELETE ON drafts
            FOR EACH ROW EXECUTE FUNCTION
            refuse_change_to_final_row('approved', 'rejected', 'invalidated', 'expired',
                                       'superseded');
        CREATE TRIGGER bookings_final_status BEFORE UPDATE OR DELETE ON bookings
            FOR EACH ROW EXECUTE FUNCTION refuse_change_to_final_row('cancelled', 'expired');
        CREATE TRIGGER outbox_final_status BEFORE UPDATE OR DELETE ON outbox
            FOR EACH ROW EXECUTE FUNCTION refuse_change_to_final_row('sent');

        -- D51, part 2: write-once tables: no UPDATE, no DELETE, whatever the status.
        CREATE FUNCTION refuse_any_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION USING
                MESSAGE = TG_TABLE_NAME || ' is write-once: ' || TG_OP || ' refused',
                ERRCODE = 'restrict_violation',
                CONSTRAINT = TG_TABLE_NAME || '_write_once';
        END;
        $$;

        CREATE TRIGGER quotes_write_once BEFORE UPDATE OR DELETE ON quotes
            FOR EACH ROW EXECUTE FUNCTION refuse_any_change();
        CREATE TRIGGER draft_versions_write_once BEFORE UPDATE OR DELETE ON draft_versions
            FOR EACH ROW EXECUTE FUNCTION refuse_any_change();
        CREATE TRIGGER draft_offers_write_once BEFORE UPDATE OR DELETE ON draft_offers
            FOR EACH ROW EXECUTE FUNCTION refuse_any_change();
        CREATE TRIGGER audit_log_write_once BEFORE UPDATE OR DELETE ON audit_log
            FOR EACH ROW EXECUTE FUNCTION refuse_any_change();

        -- D51, part 3: the outbox text never changes, even while the row is 'pending'
        -- (only status, attempts, last_error and sent_at may change).
        CREATE FUNCTION refuse_outbox_text_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION USING
                MESSAGE = 'outbox ' || OLD.id || ': the text cannot be changed',
                ERRCODE = 'restrict_violation',
                CONSTRAINT = 'outbox_text_write_once';
        END;
        $$;

        CREATE TRIGGER outbox_text_write_once BEFORE UPDATE ON outbox
            FOR EACH ROW
            WHEN (ROW(OLD.draft_id, OLD.to_address, OLD.subject, OLD.body, OLD.created_at)
                  IS DISTINCT FROM
                  ROW(NEW.draft_id, NEW.to_address, NEW.subject, NEW.body, NEW.created_at))
            EXECUTE FUNCTION refuse_outbox_text_change();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER outbox_text_write_once ON outbox;
        DROP TRIGGER audit_log_write_once ON audit_log;
        DROP TRIGGER draft_offers_write_once ON draft_offers;
        DROP TRIGGER draft_versions_write_once ON draft_versions;
        DROP TRIGGER quotes_write_once ON quotes;
        DROP TRIGGER outbox_final_status ON outbox;
        DROP TRIGGER bookings_final_status ON bookings;
        DROP TRIGGER drafts_final_status ON drafts;
        DROP TRIGGER emails_final_status ON emails;
        DROP FUNCTION refuse_outbox_text_change();
        DROP FUNCTION refuse_any_change();
        DROP FUNCTION refuse_change_to_final_row();
        DROP TABLE audit_log;
        ALTER TABLE bookings
            DROP CONSTRAINT bookings_draft_approved_fkey,
            DROP COLUMN draft_status,
            DROP COLUMN draft_id;
        DROP TABLE outbox;
        ALTER TABLE drafts DROP CONSTRAINT drafts_id_request_id_status_key;
        """
    )

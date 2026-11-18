// The API's response bodies, as the UI uses them (src/anemos/api/models.py).
// Every generated fixture is checked against these with `satisfies` (D84), and a
// Python unit test checks the status lists against anemos.states.

export const EMAIL_STATUSES = [
  "received",
  "processing",
  "set_aside",
  "escalated",
  "awaiting_approval",
  "answered",
  "expired",
  "superseded",
  "closed",
] as const;
export type EmailStatus = (typeof EMAIL_STATUSES)[number];

export const DRAFT_STATUSES = [
  "pending_approval",
  "approved",
  "rejected",
  "expired",
  "superseded",
  "invalidated",
] as const;
export type DraftStatus = (typeof DRAFT_STATUSES)[number];

export interface EmailSummary {
  id: number;
  from_address: string;
  subject: string;
  received_at: string; // ISO 8601, UTC
  status: EmailStatus;
  reason_codes: string[];
  draft_id: number | null;
  draft_status: DraftStatus | null;
  draft_past_expiry: boolean; // pending and older than 72 h (D42, D54)
}

export interface EmailPage {
  items: EmailSummary[];
  next_cursor: string | null; // pass back as `cursor`; null = last page
}

export interface DraftVersion {
  version: number;
  author: string; // "agent" or "owner" (D41)
  body: string;
  guardrail_results: Record<string, boolean>; // check name -> passed
  created_at: string;
}

export interface DraftOffer {
  quote_id: number;
  room_type_id: string;
  room_type_name: string;
  check_in: string; // YYYY-MM-DD
  check_out: string;
  rooms: number;
  hold: boolean;
  hold_allowed: boolean;
  changes: string[];
  accommodation_subtotal_cents: number;
  city_tax_cents: number;
  deposit_cents: number;
  free_cancellation_until: string;
  rendered_block: string; // exactly as version 1 showed it (D63)
}

export interface Draft {
  id: number;
  route: string;
  status: DraftStatus;
  created_at: string;
  decided_at: string | null;
  past_expiry: boolean;
  versions: DraftVersion[]; // oldest first; the last one is the current text
  offers: DraftOffer[];
}

export interface Outbox {
  id: number;
  status: "pending" | "sent";
  attempts: number;
  last_error: string | null;
  next_attempt_at: string | null;
  sent_at: string | null;
}

export interface EmailDetail {
  id: number;
  message_id: string;
  from_address: string;
  subject: string;
  raw_body: string;
  clean_body: string;
  language: string | null;
  received_at: string;
  status: EmailStatus;
  reason_codes: string[];
  escalation_summary: string | null; // written by a model (D81): the UI says so
  draft: Draft | null;
  outbox: Outbox | null;
}

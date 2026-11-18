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

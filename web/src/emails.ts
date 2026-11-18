import { call } from "./api";
import type { EmailPage, EmailStatus } from "./types";

export const PAGE_SIZE = 50;

/** One inbox page, newest first. No status: every email but set-aside ones (D50). */
export async function fetchInbox(
  status: EmailStatus | null,
  cursor: string | null,
  limit: number = PAGE_SIZE,
): Promise<EmailPage> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (status !== null) query.set("status", status);
  if (cursor !== null) query.set("cursor", cursor);
  return (await call("GET", `/emails?${query.toString()}`)) as EmailPage;
}

export const STATUS_LABEL: Record<EmailStatus, string> = {
  received: "Received",
  processing: "Processing",
  set_aside: "Set aside",
  escalated: "Escalated",
  awaiting_approval: "Awaiting approval",
  answered: "Answered",
  expired: "Expired",
  superseded: "Superseded",
  closed: "Closed",
};

// The owner thinks in hotel time (D23).
const HOTEL_TIME = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/Athens",
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

export function hotelTime(iso: string): string {
  return HOTEL_TIME.format(new Date(iso));
}

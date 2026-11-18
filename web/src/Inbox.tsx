import { useEffect, useState } from "react";

import { ApiError } from "./api";
import { PAGE_SIZE, STATUS_LABEL, fetchInbox, hotelTime } from "./emails";
import { Link, navigate, useLocation } from "./router";
import { EMAIL_STATUSES, type EmailStatus, type EmailSummary } from "./types";

interface Props {
  onUnauthenticated: () => void;
  pageSize?: number;
}

function isStatus(value: string | null): value is EmailStatus {
  return (EMAIL_STATUSES as readonly (string | null)[]).includes(value);
}

/** The inbox with its status filter, kept in the URL (?status=) so Back from an
 * email returns to the same filter; changing it starts a fresh list. */
export function Inbox({ onUnauthenticated, pageSize = PAGE_SIZE }: Props) {
  const requested = useLocation().searchParams.get("status");
  const status = isStatus(requested) ? requested : null;
  return (
    <section>
      <div className="toolbar">
        <h1>Inbox</h1>
        <label>
          Show{" "}
          <select
            value={status ?? ""}
            onChange={(event) => {
              const value = event.target.value;
              navigate(value === "" ? "/" : `/?status=${value}`, { replace: true });
            }}
          >
            <option value="">All but set aside</option>
            {EMAIL_STATUSES.map((s) => (
              <option key={s} value={s}>
                {STATUS_LABEL[s]}
              </option>
            ))}
          </select>
        </label>
      </div>
      <EmailList
        key={status ?? "all"}
        status={status}
        pageSize={pageSize}
        onUnauthenticated={onUnauthenticated}
      />
    </section>
  );
}

function EmailList({
  status,
  pageSize,
  onUnauthenticated,
}: {
  status: EmailStatus | null;
  pageSize: number;
  onUnauthenticated: () => void;
}) {
  const [items, setItems] = useState<EmailSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [failure, setFailure] = useState<string | null>(null);

  function failed(error: unknown) {
    if (isUnauthenticated(error)) onUnauthenticated();
    else setFailure(failureText(error));
  }

  async function load(after: string | null) {
    setLoading(true);
    setFailure(null);
    try {
      const page = await fetchInbox(status, after, pageSize);
      setItems((shown) => (after === null ? page.items : [...shown, ...page.items]));
      setCursor(page.next_cursor);
    } catch (error) {
      failed(error);
    } finally {
      setLoading(false);
    }
  }

  // The first page (the parent remounts the list on a filter change).
  useEffect(() => {
    let current = true;
    fetchInbox(status, null, pageSize).then(
      (page) => {
        if (!current) return;
        setItems(page.items);
        setCursor(page.next_cursor);
        setLoading(false);
      },
      (error: unknown) => {
        if (!current) return;
        setLoading(false);
        if (isUnauthenticated(error)) onUnauthenticated();
        else setFailure(failureText(error));
      },
    );
    return () => {
      current = false;
    };
  }, [status, pageSize, onUnauthenticated]);

  return (
    <>
      {failure !== null && (
        <p className="error" role="alert">
          {failure}{" "}
          <button type="button" onClick={() => void load(null)}>
            Try again
          </button>
        </p>
      )}
      {!loading && failure === null && items.length === 0 && (
        <p className="muted">{status === null ? "No emails." : "No emails with this status."}</p>
      )}
      {items.length > 0 && (
        <ul className="emails" aria-label="Emails">
          {items.map((email) => (
            <EmailRow key={email.id} email={email} />
          ))}
        </ul>
      )}
      {loading && <p className="muted">Loading…</p>}
      {!loading && cursor !== null && (
        <button type="button" className="secondary" onClick={() => void load(cursor)}>
          Load more
        </button>
      )}
    </>
  );
}

function isUnauthenticated(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

function failureText(error: unknown): string {
  const detail = error instanceof ApiError ? ` (HTTP ${String(error.status)})` : "";
  return `Could not load the inbox${detail}.`;
}

function EmailRow({ email }: { email: EmailSummary }) {
  return (
    <li className="email">
      <div className="email-main">
        <Link to={`/emails/${String(email.id)}`} className="subject">
          {email.subject}
        </Link>
        <span className="muted">{email.from_address}</span>
      </div>
      <div className="email-meta">
        <span className={`badge status-${email.status}`}>{STATUS_LABEL[email.status]}</span>
        {email.reason_codes.map((code) => (
          <span key={code} className="badge reason">
            {code}
          </span>
        ))}
        {email.draft_past_expiry && (
          <span className="badge overdue" title="No action for 72 h: the next action expires it">
            Past 72 h
          </span>
        )}
        <time className="muted" dateTime={email.received_at}>
          {hotelTime(email.received_at)}
        </time>
      </div>
    </li>
  );
}

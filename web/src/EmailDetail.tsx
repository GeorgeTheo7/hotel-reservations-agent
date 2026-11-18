import { useEffect, useState } from "react";

import { ApiError, call } from "./api";
import { STATUS_LABEL, hotelTime } from "./emails";
import { Link } from "./router";
import type { Draft, DraftOffer, DraftVersion, EmailDetail as Detail, Outbox } from "./types";

export async function fetchEmail(id: number): Promise<Detail> {
  return (await call("GET", `/emails/${String(id)}`)) as Detail;
}

// What each change on an offer means for the guest (the quote's `changes`, D62).
const CHANGE_LABEL: Record<string, string> = {
  open_part: "only the open nights",
  min_stay: "lengthened to the minimum stay",
  other_type: "another room type",
  more_rooms: "more rooms",
  other_dates: "other dates",
  window_option: "one option in the guest's period",
};

// Checks that can fail on an owner's edit, as warnings to confirm (D63, D77).
const CHECK_LABEL: Record<string, string> = {
  price_check: "a price block was changed",
  g2_numbers_from_policies: "a number not taken from the policies",
  g3_summary_present: "the request summary line was changed or removed",
  g4_reply_script: "not in the guest's script",
  g5_no_forbidden_phrases: "a forbidden phrase",
};

const ROUTE_LABEL: Record<string, string> = {
  quote: "Quote",
  ask_info: "Asks for information",
  policy: "Policy answer",
  decline: "Decline",
};

type State = { kind: "loading" } | { kind: "found"; email: Detail } | { kind: "failed"; message: string };

export function EmailDetail({ id, onUnauthenticated }: { id: number; onUnauthenticated: () => void }) {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let current = true;
    fetchEmail(id).then(
      (email) => {
        if (current) setState({ kind: "found", email });
      },
      (error: unknown) => {
        if (!current) return;
        if (error instanceof ApiError && error.status === 401) onUnauthenticated();
        else if (error instanceof ApiError && error.status === 404)
          setState({ kind: "failed", message: "There is no such email." });
        else setState({ kind: "failed", message: "Could not load this email." });
      },
    );
    return () => {
      current = false;
    };
  }, [id, onUnauthenticated]);

  return (
    <article className="detail">
      <Link to="/" className="back">
        ← Inbox
      </Link>
      {state.kind === "loading" && <p className="muted">Loading…</p>}
      {state.kind === "failed" && (
        <p className="error" role="alert">
          {state.message}
        </p>
      )}
      {state.kind === "found" && <Found email={state.email} />}
    </article>
  );
}

function Found({ email }: { email: Detail }) {
  return (
    <>
      <header>
        <h1>{email.subject}</h1>
        <p className="muted">
          {email.from_address} · <time dateTime={email.received_at}>{hotelTime(email.received_at)}</time>
          {email.language !== null && ` · ${email.language.toUpperCase()}`}
        </p>
        <p>
          <span className={`badge status-${email.status}`}>{STATUS_LABEL[email.status]}</span>{" "}
          {email.reason_codes.map((code) => (
            <span key={code} className="badge reason">
              {code}
            </span>
          ))}
        </p>
      </header>
      {email.status === "escalated" && <Summary text={email.escalation_summary} />}
      <section aria-labelledby="guest-email">
        <h2 id="guest-email">Guest's email</h2>
        <pre className="text">{email.clean_body}</pre>
      </section>
      {email.draft !== null && <DraftView draft={email.draft} />}
      {email.outbox !== null && <OutboxView outbox={email.outbox} />}
    </>
  );
}

/** D81: the summary is the model's reading of the email, so the owner is told so. */
function Summary({ text }: { text: string | null }) {
  return (
    <aside className="summary" aria-label="Summary written by the model">
      <p className="summary-label">Summary written by the model. Check it against the email below.</p>
      {text === null ? <p className="muted">No summary for this email.</p> : <p>{text}</p>}
    </aside>
  );
}

function DraftView({ draft }: { draft: Draft }) {
  const current = draft.versions.at(-1);
  if (current === undefined) return null;
  const warnings = Object.entries(current.guardrail_results)
    .filter(([check, passed]) => !passed && check in CHECK_LABEL)
    .map(([check]) => CHECK_LABEL[check] ?? check);
  return (
    <section aria-labelledby="draft">
      <h2 id="draft">
        Draft reply · version {current.version}
        {current.author === "owner" ? " (edited by you)" : " (by the agent)"}
      </h2>
      <p className="muted">
        {ROUTE_LABEL[draft.route] ?? draft.route} · {draft.status.replace("_", " ")}
        {draft.past_expiry && (
          <>
            {" "}
            <span className="badge overdue">Past 72 h: the next action expires it</span>
          </>
        )}
      </p>
      {warnings.length > 0 && (
        <div className="notice" role="note">
          This version needs confirming before it can be sent: {warnings.join("; ")}.
        </div>
      )}
      <pre className="text">{current.body}</pre>
      {draft.offers.length > 0 && (
        <>
          <h3>Offers</h3>
          {draft.offers.map((offer) => (
            <OfferView key={offer.quote_id} offer={offer} />
          ))}
        </>
      )}
      {draft.versions.length > 1 && <History versions={draft.versions} />}
    </section>
  );
}

function OfferView({ offer }: { offer: DraftOffer }) {
  const changes = offer.changes.map((c) => CHANGE_LABEL[c] ?? c);
  return (
    <div className="offer">
      <p>
        <strong>{offer.room_type_name}</strong>, {offer.rooms} {offer.rooms === 1 ? "room" : "rooms"},{" "}
        {offer.check_in} to {offer.check_out}
        {" · "}
        {offer.hold ? (
          <span className="badge">Held at approval (48 h)</span>
        ) : (
          <span className="badge">Not held</span>
        )}
      </p>
      {changes.length > 0 && <p className="muted">Differs from the request: {changes.join(", ")}.</p>}
      <pre className="text">{offer.rendered_block}</pre>
    </div>
  );
}

function History({ versions }: { versions: DraftVersion[] }) {
  return (
    <details className="history">
      <summary>All versions ({versions.length})</summary>
      <ol>
        {versions.map((v) => (
          <li key={v.version}>
            <p className="muted">
              Version {v.version}, {v.author === "owner" ? "by you" : "by the agent"},{" "}
              <time dateTime={v.created_at}>{hotelTime(v.created_at)}</time>
            </p>
            <pre className="text">{v.body}</pre>
          </li>
        ))}
      </ol>
    </details>
  );
}

function OutboxView({ outbox }: { outbox: Outbox }) {
  return (
    <section aria-labelledby="outbox">
      <h2 id="outbox">Reply</h2>
      {outbox.status === "sent" && outbox.sent_at !== null ? (
        <p>
          Sent (simulated) <time dateTime={outbox.sent_at}>{hotelTime(outbox.sent_at)}</time>.
        </p>
      ) : (
        <p>
          Waiting to be sent{outbox.attempts > 0 && `, ${String(outbox.attempts)} failed attempts`}
          {outbox.last_error !== null && ` (last error: ${outbox.last_error})`}.
        </p>
      )}
    </section>
  );
}

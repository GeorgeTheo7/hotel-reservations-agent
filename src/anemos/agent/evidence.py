"""A resumed run's evidence, rebuilt from the database (D75, owner decision, NOTES
2026-11-17).

The graph state keeps only ids; the final write needs the objects: the `Hit`s
(guardrails 2 and 6 check against their text) and the `StoredQuote`s (the quote
blocks are rendered from them). A run that never stops keeps the objects its tools
returned. A resumed run rebuilds them here, refusing on any difference:

- **Hits** from the run's own `agent.tool_called` rows for `search_policies`, only if
  each row's `policies_sha256` equals the packaged `policies.md` now.
- **Quotes** priced again from the quote row and the request (as approval does, D47);
  every amount and line must equal the stored one.

This reverses the step-8 note ("a stored quote row cannot rebuild a block") on resume
only, guarded by that equality check. A refusal is a reason string; the caller ends
the run SYSTEM_ERROR.
"""

from collections.abc import Sequence

import psycopg
from psycopg.rows import TupleRow

from anemos.audit import AuditEvent
from anemos.policies.embeddings import policies_sha256
from anemos.policies.search import Hit
from anemos.repricing import amounts, price_again
from anemos.reservations.errors import NeedsInfo
from anemos.reservations.loader import load_hotel_data
from anemos.reservations.model import Stay
from anemos.reservations.offer import Offer, OfferChange
from anemos.reservations.tools import LoadedHoliday, StoredQuote, load_request

type Conn = psycopg.Connection[TupleRow]


def rebuild_hits(conn: Conn, email_id: int, policy_ids: Sequence[str]) -> dict[str, Hit] | str:
    if not policy_ids:
        return {}
    rows = conn.execute(
        "SELECT output -> 'record' FROM audit_log WHERE email_id = %s AND event = %s"
        " AND output ->> 'tool' = 'search_policies' AND output -> 'record' ? 'hits'"
        " ORDER BY id",
        (email_id, AuditEvent.AGENT_TOOL_CALLED.value),
    ).fetchall()
    current = policies_sha256()
    hits: dict[str, Hit] = {}
    for (record,) in rows:
        if record["policies_sha256"] != current:
            return (f"policies.md changed since the run searched it"
                    f" ({record['policies_sha256'][:12]} -> {current[:12]})")  # fmt: skip
        for h in record["hits"]:
            hits[h["policy_id"]] = Hit(h["policy_id"], h["title"], h["text"], h["text_sha256"])
    missing = sorted(set(policy_ids) - hits.keys())
    if missing:
        return f"no recorded search returned {', '.join(missing)}"
    return {i: hits[i] for i in policy_ids}


def rebuild_quotes(
    conn: Conn, request_id: int | None, quote_ids: Sequence[int]
) -> dict[int, StoredQuote] | str:
    if not quote_ids:
        return {}
    if request_id is None:
        return "quotes without a request"
    loaded = load_request(conn, request_id)
    if isinstance(loaded, (NeedsInfo, LoadedHoliday)):
        return f"request {request_id} cannot be priced ({type(loaded).__name__})"
    hotel = load_hotel_data(conn)
    quotes: dict[int, StoredQuote] = {}
    for quote_id in quote_ids:
        row = conn.execute(
            "SELECT room_type_id, lower(stay), upper(stay), rooms, changes, lines,"
            " room_subtotal_cents, extra_bed_subtotal_cents, accommodation_subtotal_cents,"
            " city_tax_cents, deposit_cents, free_cancellation_until"
            " FROM quotes WHERE id = %s AND request_id = %s",
            (quote_id, request_id),
        ).fetchone()
        if row is None:
            return f"quote {quote_id} is not a quote of request {request_id}"
        fresh = price_again(hotel, loaded.party, quote_id, str(row[0]), Stay(row[1], row[2]),
                            int(row[3]), amounts(row[5], *row[6:12]))  # fmt: skip
        if isinstance(fresh, str):
            return fresh
        changes = frozenset(OfferChange(c) for c in row[4])
        quotes[quote_id] = StoredQuote(quote_id, Offer(fresh, changes))
    return quotes

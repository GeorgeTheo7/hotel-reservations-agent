"""A stored quote priced again from the database, compared line by line.

Used by approval's re-validation (D47) and by a resumed run, which rebuilds its quotes
from their rows (D75, NOTES 2026-11-17): in both, the fresh price must equal the stored
amounts, or the caller refuses to go on.
"""

from datetime import date
from typing import Any

from anemos.reservations.errors import NeedsEscalation
from anemos.reservations.hotel_data import HotelData
from anemos.reservations.model import Party, Stay
from anemos.reservations.pricing import Quote, price_stay
from anemos.reservations.tools import line_json

AMOUNT_COLUMNS = (
    "room_subtotal_cents", "extra_bed_subtotal_cents", "accommodation_subtotal_cents",
    "city_tax_cents", "deposit_cents", "free_cancellation_until",
)  # fmt: skip


def amounts(lines: object, *values: object) -> dict[str, Any]:
    """A quote's lines and amounts, in the shape stored and compared."""
    return {"lines": lines, **dict(zip(AMOUNT_COLUMNS, values, strict=True))}


def quote_amounts(quote: Quote) -> dict[str, Any]:
    free_until: date = quote.free_cancellation_until
    return amounts(
        [line_json(line) for line in quote.lines],
        quote.room_subtotal_cents, quote.extra_bed_subtotal_cents,
        quote.accommodation_subtotal_cents, quote.city_tax_cents, quote.deposit_cents,
        free_until,
    )  # fmt: skip


def price_again(
    hotel: HotelData, party: Party, quote_id: int, room_type_id: str, stay: Stay, rooms: int,
    stored: dict[str, Any],
) -> Quote | str:  # fmt: skip
    """The quote priced now, if it equals `stored`; else why not (a reason string)."""
    fresh = price_stay(hotel, room_type_id, stay, party, rooms)
    if not isinstance(fresh, Quote):
        why = fresh.reason.value if isinstance(fresh, NeedsEscalation) else fresh.code
        return f"quote {quote_id}: cannot be priced any more ({why})"
    now_amounts = quote_amounts(fresh)
    if now_amounts != stored:
        changed = sorted(k for k in now_amounts if now_amounts[k] != stored[k])
        return f"quote {quote_id}: price changed ({', '.join(changed)})"
    return fresh

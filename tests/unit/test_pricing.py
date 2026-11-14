"""Pricing against the 12 hand-calculated examples in docs/pricing-examples.md.

The expected numbers below are copied from that document (in cents), not from the
code. If one disagrees with the code: stop, compare both calculations, do not edit
the example.
"""

from dataclasses import dataclass, fields
from datetime import date

import pytest

from anemos.db.seed import hotel_data
from anemos.reservations.errors import NeedsEscalation, NoRate, OverCapacity, ReasonCode
from anemos.reservations.model import ExtraBedBand, Party, RoomOffer, RoomRequest, Season, Stay
from anemos.reservations.offer import Offer, build_offer
from anemos.reservations.pricing import Quote, price_stay
from anemos.reservations.stay_rules import hold_allowed

pytestmark = pytest.mark.unit

HOTEL = hotel_data()


def d(month: int, day: int) -> date:
    return date(2027, month, day)


@dataclass(frozen=True)
class Example:
    id: str
    # the request
    room_type_id: str
    stay: Stay
    party: Party
    rooms: int
    # the expected result, from the document
    offered_type_id: str
    offered_stay: Stay
    room_subtotal: int
    extra_beds: int
    subtotal: int
    city_tax: int
    deposit: int
    cancel_until: date
    hold: bool


EXAMPLES = [
    Example("ex01", "standard", Stay(d(5, 20), d(5, 21)), Party.of(2), 1,
            "standard", Stay(d(5, 20), d(5, 21)),
            7_000, 0, 7_000, 150, 2_100, d(5, 6), True),
    Example("ex02", "sea_view", Stay(d(7, 12), d(7, 16)), Party.of(2, (8,)), 1,
            "sea_view", Stay(d(7, 12), d(7, 16)),
            58_000, 8_000, 66_000, 1_600, 19_800, d(6, 28), True),
    Example("ex03", "standard", Stay(d(5, 29), d(6, 3)), Party.of(2), 1,
            "standard", Stay(d(5, 29), d(6, 3)),
            40_000, 0, 40_000, 1_250, 12_000, d(5, 15), True),
    Example("ex04", "sea_view", Stay(d(8, 10), d(8, 13)), Party.of(2), 1,
            "sea_view", Stay(d(8, 10), d(8, 15)),
            95_000, 0, 95_000, 2_000, 28_500, d(7, 11), False),
    Example("ex05", "standard", Stay(d(9, 1), d(9, 4)), Party.of(2, (1,)), 1,
            "standard", Stay(d(9, 1), d(9, 4)),
            28_500, 0, 28_500, 1_200, 8_600, d(8, 18), True),
    Example("ex06", "family_suite", Stay(d(7, 20), d(7, 24)), Party.of(2, (13, 6)), 1,
            "family_suite", Stay(d(7, 20), d(7, 24)),
            104_000, 22_000, 126_000, 1_600, 37_800, d(6, 20), True),
    Example("ex07", "standard", Stay(d(10, 30), date(2027, 11, 2)), Party.of(2), 1,
            "standard", Stay(d(10, 30), date(2027, 11, 1)),
            14_000, 0, 14_000, 300, 4_200, d(10, 16), False),
    Example("ex08", "sea_view", Stay(d(6, 5), d(6, 8)), Party.of(4), 1,
            "family_suite", Stay(d(6, 5), d(6, 8)),
            54_000, 21_000, 75_000, 1_200, 22_500, d(5, 22), False),
    Example("ex09", "sea_view", Stay(d(10, 3), d(10, 5)), Party.of(1, (13, 8)), 1,
            "sea_view", Stay(d(10, 3), d(10, 5)),
            19_000, 4_000, 23_000, 300, 6_900, d(9, 19), True),
    Example("ex10", "standard", Stay(d(7, 13), d(7, 17)), Party.of(2), 1,
            "standard", Stay(d(7, 13), d(7, 17)),
            47_000, 0, 47_000, 1_600, 14_100, d(6, 29), True),
    Example("ex11", "standard", Stay(d(8, 24), d(8, 27)), Party.of(4), 2,
            "standard", Stay(d(8, 24), d(8, 27)),
            84_000, 0, 84_000, 2_400, 25_200, d(7, 25), True),
    Example("ex12", "sea_view", Stay(d(10, 4), d(10, 7)), Party.of(2, (16, 12, 4)), 2,
            "sea_view", Stay(d(10, 4), d(10, 7)),
            57_000, 6_000, 63_000, 900, 18_900, d(9, 20), True),
]  # fmt: skip


def built_offer(ex: Example) -> Offer:
    """The production request-to-offer path (anemos.reservations.offer.build_offer)."""
    offer = build_offer(HOTEL, RoomRequest(ex.stay, ex.rooms, ex.room_type_id), ex.party)
    assert isinstance(offer, Offer), offer
    return offer


@pytest.mark.parametrize("ex", EXAMPLES, ids=lambda ex: ex.id)
def test_example(ex: Example) -> None:
    offer = built_offer(ex)
    quote = offer.quote
    got = {
        "offered_type_id": quote.room_type_id,
        "offered_stay": quote.stay,
        "room_subtotal": quote.room_subtotal_cents,
        "extra_beds": quote.extra_bed_subtotal_cents,
        "subtotal": quote.accommodation_subtotal_cents,
        "city_tax": quote.city_tax_cents,
        "deposit": quote.deposit_cents,
        "cancel_until": quote.free_cancellation_until,
        "hold": offer.hold_allowed,
    }
    expected = {key: getattr(ex, key) for key in got}
    assert got == expected


@pytest.mark.parametrize("ex", EXAMPLES, ids=lambda ex: ex.id)
def test_every_amount_is_int(ex: Example) -> None:
    quote = built_offer(ex).quote
    amounts = [getattr(quote, f.name) for f in fields(quote) if f.name.endswith("_cents")]
    for line in quote.lines:
        amounts += [getattr(line, f.name) for f in fields(line) if f.name.endswith("_cents")]
    assert len(amounts) >= 5
    assert all(type(cents) is int for cents in amounts)


@pytest.mark.parametrize("ex", EXAMPLES, ids=lambda ex: ex.id)
def test_offer_hold_agrees_with_stay_rules_hold_allowed(ex: Example) -> None:
    """Two statements of D9 must agree for a single offer: "no changes" (offer.py) and
    "same dates, rooms and type" (stay_rules.hold_allowed)."""
    offer = built_offer(ex)
    q = offer.quote
    requested = RoomRequest(ex.stay, ex.rooms, ex.room_type_id)
    assert offer.hold_allowed == hold_allowed(
        requested, [RoomOffer(q.stay, q.rooms, q.room_type_id)]
    )


def test_totals_are_sums_of_the_nightly_lines() -> None:
    for ex in EXAMPLES:
        q = built_offer(ex).quote
        assert q.room_subtotal_cents == sum(line.room_cents for line in q.lines)
        assert q.extra_bed_subtotal_cents == sum(line.extra_bed_cents for line in q.lines)
        assert q.city_tax_cents == sum(line.city_tax_cents for line in q.lines)
        assert q.accommodation_subtotal_cents == q.room_subtotal_cents + q.extra_bed_subtotal_cents


# --- the nightly lines are kept in the Quote ---


def test_ex02_nightly_lines() -> None:
    q = price_stay(HOTEL, "sea_view", Stay(d(7, 12), d(7, 16)), Party.of(2, (8,)))
    assert isinstance(q, Quote)
    assert [
        (line.night, line.season, line.room_cents, line.extra_bed_cents) for line in q.lines
    ] == [
        (d(7, 12), Season.MID, 13_000, 2_000),
        (d(7, 13), Season.MID, 13_000, 2_000),
        (d(7, 14), Season.MID, 13_000, 2_000),
        (d(7, 15), Season.HIGH, 19_000, 2_000),
    ]
    assert q.extra_bed_bands == (ExtraBedBand.CHILD_2_11,)


def test_ex03_city_tax_changes_with_the_season() -> None:
    q = price_stay(HOTEL, "standard", Stay(d(5, 29), d(6, 3)), Party.of(2))
    assert isinstance(q, Quote)
    assert [line.city_tax_cents for line in q.lines] == [150, 150, 150, 400, 400]


def test_ex11_two_rooms_multiply_room_and_tax_per_night() -> None:
    q = price_stay(HOTEL, "standard", Stay(d(8, 24), d(8, 27)), Party.of(4), rooms=2)
    assert isinstance(q, Quote)
    first = q.lines[0]
    assert (first.room_rate_cents, first.room_cents, first.city_tax_cents) == (14_000, 28_000, 800)


# --- what pricing refuses ---


def test_over_capacity() -> None:
    result = price_stay(HOTEL, "sea_view", Stay(d(6, 5), d(6, 8)), Party.of(4))
    assert result == OverCapacity(head_count=4, max_people=3)


def test_closed_nights_have_no_rate() -> None:
    result = price_stay(HOTEL, "standard", Stay(d(10, 30), date(2027, 11, 2)), Party.of(2))
    assert result == NoRate((date(2027, 11, 1),))


def test_three_rooms_is_a_group() -> None:
    result = price_stay(HOTEL, "standard", Stay(d(5, 20), d(5, 21)), Party.of(6), rooms=3)
    assert isinstance(result, NeedsEscalation)
    assert result.reason == ReasonCode.GROUP


def test_fewer_adults_than_rooms_escalates() -> None:
    party = Party.of(1, (15, 12))
    result = price_stay(HOTEL, "standard", Stay(d(5, 20), d(5, 21)), party, rooms=2)
    assert isinstance(result, NeedsEscalation)
    assert result.reason == ReasonCode.UNACCOMPANIED_CHILDREN


def test_unknown_room_type_is_a_bug() -> None:
    with pytest.raises(ValueError, match="unknown room type"):
        price_stay(HOTEL, "penthouse", Stay(d(5, 20), d(5, 21)), Party.of(2))

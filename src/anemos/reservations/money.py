"""Money as integer cents (D14). No floats anywhere in `reservations/`.

Every amount is an `int` number of euro cents: €1.50 is 150. Integer arithmetic is
exact, so a total is always the exact sum of its lines. `tests/unit/test_no_float.py`
fails the build if float-producing code appears in this package.
"""

from typing import NewType

# A distinct name for "an int that means cents". mypy rejects a float where Cents is
# expected; at runtime it is a plain int.
Cents = NewType("Cents", int)

CENTS_PER_EURO = 100
DEPOSIT_PERCENT = 30


def deposit_cents(accommodation_subtotal: Cents) -> Cents:
    """Deposit = 30% of the accommodation subtotal, rounded up to a whole euro (D14, D24).

    Example 5: subtotal 28_500 (€285) -> 30% = €85.50 -> deposit 8_600 (€86).
    """
    _require_cents(accommodation_subtotal)
    if accommodation_subtotal < 0:
        raise ValueError(f"subtotal must not be negative, got {accommodation_subtotal}")
    # 30% of `subtotal` cents, in whole euros, is subtotal * 30 / (100 * 100).
    # ceil(a / b) for integers is -(-a // b): floor division of the negated value.
    whole_euros = -(-accommodation_subtotal * DEPOSIT_PERCENT // (100 * CENTS_PER_EURO))
    return Cents(whole_euros * CENTS_PER_EURO)


def _require_cents(amount: object) -> None:
    """Reject floats (and bools, which are ints in Python) at runtime too."""
    if type(amount) is not int:
        raise TypeError(f"money must be int cents, got {type(amount).__name__}: {amount!r}")

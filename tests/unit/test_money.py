import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from anemos.reservations.money import Cents, deposit_cents

pytestmark = pytest.mark.unit

# Subtotals from €0 to €100,000, in cents.
subtotals = st.integers(min_value=0, max_value=10_000_000)

# derandomize: the same inputs on every run, so CI is reproducible (D20 spirit).
# database=None: no .hypothesis/ folder written into the repo.
reproducible = settings(derandomize=True, database=None)


@pytest.mark.parametrize(
    ("subtotal", "expected"),
    [
        (0, 0),
        (7_000, 2_100),  # Ex 1: €70 -> €21 (exact)
        (28_500, 8_600),  # Ex 5: €285 -> €85.50 -> €86
        (126_000, 37_800),  # Ex 6: €1260 -> €378
        (1, 100),  # any part of a euro rounds up
        (33_333, 10_000),  # €333.33 -> €99.9999 -> €100
        (33_334, 10_100),  # €333.34 -> €100.0002 -> €101
    ],
)
def test_deposit_examples(subtotal: int, expected: int) -> None:
    result = deposit_cents(Cents(subtotal))
    assert result == expected
    assert type(result) is int


@pytest.mark.parametrize("bad", [21.0, 0.3, True, "7000"])
def test_deposit_rejects_non_int(bad: object) -> None:
    with pytest.raises(TypeError, match="int cents"):
        deposit_cents(bad)  # type: ignore[arg-type]


def test_deposit_rejects_negative() -> None:
    with pytest.raises(ValueError, match="negative"):
        deposit_cents(Cents(-1))


# Properties: rules that must hold for every subtotal, checked on many generated inputs.


@reproducible
@given(subtotals)
def test_deposit_is_whole_euros(subtotal: int) -> None:
    assert deposit_cents(Cents(subtotal)) % 100 == 0


@reproducible
@given(subtotals)
def test_deposit_is_at_least_30_percent_and_less_than_one_euro_more(subtotal: int) -> None:
    deposit = deposit_cents(Cents(subtotal))
    # Multiply both sides by 100 instead of dividing: 30% of s <= d < 30% of s + €1.
    assert deposit * 100 >= subtotal * 30
    assert deposit * 100 < subtotal * 30 + 100 * 100


@reproducible
@given(subtotals, subtotals)
def test_deposit_never_decreases_when_subtotal_grows(a: int, b: int) -> None:
    low, high = sorted((a, b))
    assert deposit_cents(Cents(low)) <= deposit_cents(Cents(high))

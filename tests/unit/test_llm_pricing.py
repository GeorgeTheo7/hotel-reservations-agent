"""Model prices and call costs (D2), integer micro-dollars."""

import pytest

from anemos.llm.pricing import EMBEDDING_MODELS, PRICES, UnknownModel, Usage, cost, max_cost

pytestmark = pytest.mark.unit


def test_cost_bills_cached_input_and_output_at_their_own_prices() -> None:
    """gpt-6-luna: 800 uncached x $0.10/M = 80 µ$, 200 cached x $0.01/M = 2 µ$,
    500 output (reasoning included) x $0.50/M = 250 µ$."""
    usage = Usage(input_tokens=1_000, cached_input_tokens=200, output_tokens=500)
    assert cost("gpt-6-luna", usage) == 80 + 2 + 250


def test_every_part_rounds_up_so_cost_is_never_underestimated() -> None:
    """One token at $0.10/M is 0.1 µ$: billed as 1 µ$, never 0."""
    assert cost("gpt-6-luna", Usage(1, 0, 0)) == 1
    assert cost("gpt-6-luna", Usage(1, 1, 1)) == 0 + 1 + 1


def test_max_cost_counts_every_input_token_as_uncached() -> None:
    assert max_cost("gpt-5.4-mini", max_input_tokens=2_000, max_output_tokens=1_000) == (
        cost("gpt-5.4-mini", Usage(2_000, 0, 1_000))
    )
    assert max_cost("gpt-5.4-mini", max_input_tokens=2_000, max_output_tokens=1_000) == (
        1_500 + 4_500
    )


def test_an_unknown_model_is_refused() -> None:
    with pytest.raises(UnknownModel, match="gpt-5-nano"):
        cost("gpt-5-nano", Usage(1, 0, 1))
    with pytest.raises(UnknownModel):
        max_cost("no-such-model", max_input_tokens=1, max_output_tokens=1)


@pytest.mark.parametrize(
    "counts", [(-1, 0, 0), (10, 0, -1), (10, 11, 0)], ids=["negative", "negative-out", "cached>in"]
)
def test_impossible_usage_is_refused(counts: tuple[int, int, int]) -> None:
    with pytest.raises(ValueError):
        Usage(*counts)


def test_the_price_table_is_sane() -> None:
    """Cached input is never dearer than input, output never cheaper; an embedding
    model has no output tokens and no cache discount."""
    assert set(PRICES) >= EMBEDDING_MODELS
    for model, price in PRICES.items():
        if model in EMBEDDING_MODELS:
            assert 0 < price.input == price.cached_input and price.output == 0, model
        else:
            assert 0 < price.cached_input <= price.input <= price.output, model


def test_an_embedding_costs_its_input_tokens() -> None:
    """text-embedding-3-small at $0.02/M: 4,000 tokens = 80 µ$."""
    assert cost("text-embedding-3-small", Usage(4_000, 0, 0)) == 80
    assert max_cost("text-embedding-3-small", max_input_tokens=4_000, max_output_tokens=0) == 80

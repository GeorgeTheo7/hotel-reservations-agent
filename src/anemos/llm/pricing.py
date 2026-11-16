"""Model prices and the cost of one call, in integer micro-dollars (D2).

Prices are copied from OpenAI's official pricing page on the date below (standard tier,
text tokens, USD per 1M tokens). A model not in the table is refused: its cost could
not be computed, so the budget cap (budget.py) could not hold.

Cost of a call, from the token usage the response reports:
- input tokens include cached ones; cached input is billed at the cached price,
- output tokens include reasoning tokens, billed at the output price.
Each part is rounded up to a whole micro-dollar, so a cost is never underestimated.
"""

from dataclasses import dataclass
from datetime import date

PRICES_READ_ON = date(2026, 11, 15)
PRICES_SOURCE = "https://developers.openai.com/api/docs/pricing"

TOKENS_PER_PRICE_UNIT = 1_000_000  # prices are per 1M tokens


@dataclass(frozen=True)
class Price:
    """Micro-dollars per 1M tokens ($0.10 per 1M = 100_000)."""

    input: int
    cached_input: int
    output: int


# The three candidates for the 5-email Greek check (week 4). Deprecated cheaper models
# are left out on purpose.
PRICES: dict[str, Price] = {
    "gpt-6-luna": Price(input=100_000, cached_input=10_000, output=500_000),
    "gpt-5.6-luna": Price(input=200_000, cached_input=20_000, output=1_200_000),
    "gpt-5.4-mini": Price(input=750_000, cached_input=75_000, output=4_500_000),
    # Embeddings (week 5, read 2026-11-16): input only; no cache discount, no output tokens.
    "text-embedding-3-small": Price(input=20_000, cached_input=20_000, output=0),
}
EMBEDDING_MODELS = frozenset({"text-embedding-3-small"})


class UnknownModel(LookupError):
    """No price for this model, so its cost cannot be computed."""


@dataclass(frozen=True)
class Usage:
    """Token counts as the response reports them."""

    input_tokens: int  # including cached
    cached_input_tokens: int
    output_tokens: int  # including reasoning
    reasoning_tokens: int = 0  # part of output_tokens (billed as output), for reports

    def __post_init__(self) -> None:
        counts = (self.input_tokens, self.cached_input_tokens, self.output_tokens,
                  self.reasoning_tokens)  # fmt: skip
        if min(counts) < 0:
            raise ValueError(f"negative token count: {self}")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError(f"more cached than input tokens: {self}")
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError(f"more reasoning than output tokens: {self}")


def price_of(model: str) -> Price:
    try:
        return PRICES[model]
    except KeyError:
        raise UnknownModel(f"no price for model {model!r}; add it to PRICES") from None


def _part(tokens: int, micro_per_unit: int) -> int:
    """tokens x price, rounded up to a whole micro-dollar (integer only)."""
    return -(-tokens * micro_per_unit // TOKENS_PER_PRICE_UNIT)


def cost(model: str, usage: Usage) -> int:
    """What one call cost, in micro-dollars."""
    price = price_of(model)
    uncached = usage.input_tokens - usage.cached_input_tokens
    return (
        _part(uncached, price.input)
        + _part(usage.cached_input_tokens, price.cached_input)
        + _part(usage.output_tokens, price.output)
    )


def max_cost(model: str, *, max_input_tokens: int, max_output_tokens: int) -> int:
    """The most a call can cost: every input token uncached, every allowed output token
    used. The budget reserves this before the call."""
    return cost(model, Usage(max_input_tokens, 0, max_output_tokens))

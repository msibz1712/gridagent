"""Estimated cost per model call, from token usage.

Prices are USD per 1M tokens, first-party Claude API list prices as of
2026-11. They change: check https://claude.com/pricing before relying on
them. Cache writes (5-minute TTL) cost 1.25x the input price.
Haiku 5.5's price is for prompts up to 100K tokens; ours are far smaller.
"""

from pydantic import BaseModel

from gridagent.llm.base import TokenUsage


class Price(BaseModel):
    input: float
    output: float
    cache_read: float

    @property
    def cache_write(self) -> float:
        return self.input * 1.25


PRICES_PER_MTOK: dict[str, Price] = {
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_read=0.10),
    "claude-haiku-5-5": Price(input=0.10, output=0.50, cache_read=0.01),
    # Fallback target used by server-side refusal fallback for Sonnet 5.5.
    "claude-sonnet-5": Price(input=2.00, output=10.00, cache_read=0.20),
}


def estimate_cost_usd(model: str, usage: TokenUsage) -> float | None:
    """Cost estimate in USD, or None if we don't know the model's price."""
    price = PRICES_PER_MTOK.get(model)
    if price is None:
        return None
    return (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_read_input_tokens * price.cache_read
        + usage.cache_creation_input_tokens * price.cache_write
    ) / 1_000_000

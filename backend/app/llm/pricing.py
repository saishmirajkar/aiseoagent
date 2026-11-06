"""List prices (USD per million tokens) used for cost accounting and budget checks.

Cache writes (5-minute TTL) cost 1.25x input; cache reads cost the listed cache-read price.
Unknown models are priced at the most expensive known rate so budgets stay conservative.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float

    @property
    def cache_write(self) -> float:
        return self.input * 1.25


PRICES: dict[str, Price] = {
    "claude-sonnet-5-5": Price(2.00, 10.00, 0.20),
    "claude-sonnet-5": Price(2.00, 10.00, 0.20),
    "claude-haiku-5-5": Price(0.10, 0.50, 0.01),
    "claude-opus-5-5": Price(4.00, 20.00, 0.20),
    "claude-opus-5": Price(5.00, 25.00, 0.50),
    "claude-opus-4-8": Price(5.00, 25.00, 0.50),
}
FALLBACK_PRICE = Price(5.00, 25.00, 0.50)


def price_for(model: str) -> Price:
    for k, v in PRICES.items():
        if model == k or model.startswith(k + "-"):
            return v
    return FALLBACK_PRICE


def usage_cost(model: str, input_tokens: int, output_tokens: int, cache_write: int = 0, cache_read: int = 0) -> float:
    p = price_for(model)
    return (input_tokens * p.input + output_tokens * p.output + cache_write * p.cache_write
            + cache_read * p.cache_read) / 1_000_000


def worst_case_cost(model: str, est_input_tokens: int, max_tokens: int) -> float:
    """Upper bound for a call: all input uncached-and-written, full max_tokens of output."""
    p = price_for(model)
    return (est_input_tokens * p.cache_write + max_tokens * p.output) / 1_000_000

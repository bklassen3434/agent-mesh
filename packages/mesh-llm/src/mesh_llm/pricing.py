"""Anthropic list-price table and per-call cost estimation (Phase 11a).

Single source of truth for token pricing. Every cost figure in the
``mesh.cli cost report`` command, the Langfuse cost attribution, and
``docs/cost-baseline.md`` derives from this module.

Prices are USD per *million* tokens, list pricing as published at
https://docs.claude.com/en/docs/about-claude/pricing.

⚠️  VERIFY BEFORE TRUSTING NUMBERS: list prices and the cache multipliers
change over time. Last confirmed 2026-05-29. Re-check docs.claude.com (and
the cache write/read multipliers in 11c) before relying on the figures.

Cache multipliers (5-minute ephemeral TTL):
- cache *write* (creation): 1.25x base input rate
- cache *read*:             0.10x base input rate
"""
from __future__ import annotations

from dataclasses import dataclass

from mesh_llm.usage import LLMUsage

_CACHE_WRITE_MULTIPLIER = 1.25
_CACHE_READ_MULTIPLIER = 0.10

# (input_per_mtok, output_per_mtok) in USD. Matched by model-id prefix so
# dated suffixes (claude-haiku-4-5-20251001) resolve to the family rate.
_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-opus-4": (15.0, 75.0),
    # Legacy families occasionally still routed to:
    "claude-3-5-haiku": (0.80, 4.0),
    "claude-3-5-sonnet": (3.0, 15.0),
}


@dataclass(frozen=True)
class CostBreakdown:
    input_cost: float
    output_cost: float
    cache_read_cost: float
    cache_write_cost: float

    @property
    def total_cost(self) -> float:
        return (
            self.input_cost
            + self.output_cost
            + self.cache_read_cost
            + self.cache_write_cost
        )


def _rates_for(model: str) -> tuple[float, float] | None:
    """Resolve (input, output) per-Mtok rates by longest matching prefix."""
    best: tuple[str, tuple[float, float]] | None = None
    for prefix, rates in _PRICES_PER_MTOK.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, rates)
    return best[1] if best is not None else None


def is_priced(model: str) -> bool:
    """True when we have a list price for this model (e.g. not an Ollama model)."""
    return _rates_for(model) is not None


def estimate_cost(model: str, usage: LLMUsage) -> CostBreakdown:
    """Estimate USD cost for one call. Unknown/unpriced models cost 0.0."""
    rates = _rates_for(model)
    if rates is None:
        return CostBreakdown(0.0, 0.0, 0.0, 0.0)
    input_rate, output_rate = rates
    per_token_in = input_rate / 1_000_000
    per_token_out = output_rate / 1_000_000
    return CostBreakdown(
        input_cost=usage.input_tokens * per_token_in,
        output_cost=usage.output_tokens * per_token_out,
        cache_read_cost=usage.cache_read_tokens * per_token_in * _CACHE_READ_MULTIPLIER,
        cache_write_cost=usage.cache_creation_tokens
        * per_token_in
        * _CACHE_WRITE_MULTIPLIER,
    )

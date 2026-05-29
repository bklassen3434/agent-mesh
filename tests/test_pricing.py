"""Phase 11a: list-price cost estimation."""
from __future__ import annotations

import pytest
from mesh_llm import LLMUsage, estimate_cost, is_priced


def test_haiku_input_output_cost() -> None:
    usage = LLMUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    cost = estimate_cost("claude-haiku-4-5", usage)
    assert cost.input_cost == pytest.approx(1.0)
    assert cost.output_cost == pytest.approx(5.0)
    assert cost.total_cost == pytest.approx(6.0)


def test_dated_model_suffix_resolves_to_family() -> None:
    usage = LLMUsage(input_tokens=1_000_000)
    assert estimate_cost("claude-haiku-4-5-20251001", usage).input_cost == pytest.approx(1.0)


def test_cache_multipliers() -> None:
    # cache read = 0.10x input; cache write = 1.25x input
    usage = LLMUsage(cache_read_tokens=1_000_000, cache_creation_tokens=1_000_000)
    cost = estimate_cost("claude-haiku-4-5", usage)
    assert cost.cache_read_cost == pytest.approx(0.10)
    assert cost.cache_write_cost == pytest.approx(1.25)


def test_longest_prefix_wins() -> None:
    # opus/sonnet families must not collide; sonnet is $3 in, opus $15 in.
    one_m = LLMUsage(input_tokens=1_000_000)
    assert estimate_cost("claude-sonnet-4-6", one_m).input_cost == pytest.approx(3.0)
    assert estimate_cost("claude-opus-4-8", one_m).input_cost == pytest.approx(15.0)


def test_unknown_model_is_free_and_unpriced() -> None:
    usage = LLMUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert not is_priced("qwen3:8b")
    assert estimate_cost("qwen3:8b", usage).total_cost == 0.0


def test_is_priced_for_known_model() -> None:
    assert is_priced("claude-haiku-4-5")


def test_usage_addition() -> None:
    total = LLMUsage(input_tokens=10, output_tokens=5) + LLMUsage(
        input_tokens=3, cache_read_tokens=7
    )
    assert total.input_tokens == 13
    assert total.output_tokens == 5
    assert total.cache_read_tokens == 7
    assert total.total_tokens == 25

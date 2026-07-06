"""Phase 20a — RoutingConfig + classify_difficulty (pure policy, no clients)."""
from __future__ import annotations

import pytest
from mesh_llm.routing import (
    RoutingConfig,
    Tier,
    classify_difficulty,
    has_static_model_override,
)

_ROUTE_VARS = (
    "MESH_ROUTE_ENABLED",
    "MESH_ROUTE_CHEAP_MODEL",
    "MESH_ROUTE_STRONG_MODEL",
    "MESH_ROUTE_CHEAP_PROVIDER",
    "MESH_ROUTE_STRONG_PROVIDER",
    "MESH_ROUTE_ESCALATE_CHARS",
    "MESH_ROUTE_ESCALATE_ON_PARSE_FAIL",
    "MESH_ROUTE_SKEPTIC_ENABLED",
    "MESH_ROUTE_EXTRACTION_ENABLED",
    "MESH_LLM_MODEL",
    "MESH_LLM_MODEL_DEFAULT",
    "MESH_LLM_MODEL_SKEPTIC",
    "MESH_LLM_MODEL_EXTRACTION",
    "MESH_LLM_MODEL_SKEPTIC_STRONG",
    "MESH_LLM_PROVIDER",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ROUTE_VARS:
        monkeypatch.delenv(var, raising=False)


# ── defaults ────────────────────────────────────────────────────────────────


def test_defaults_off_with_documented_values() -> None:
    cfg = RoutingConfig.from_env()
    assert cfg.enabled is False
    assert cfg.cheap_model == "claude-haiku-4-5"
    assert cfg.strong_model == "claude-sonnet-4-6"
    assert cfg.cheap_provider == "anthropic"
    assert cfg.strong_provider == "anthropic"
    assert cfg.escalate_chars == 12_000
    assert cfg.escalate_on_parse_fail is True


def test_cheap_model_default_follows_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_LLM_PROVIDER", "ollama")
    cfg = RoutingConfig.from_env()
    assert cfg.cheap_provider == "ollama"
    assert cfg.cheap_model == "qwen3:8b"
    # Strong defaults to the documented Anthropic model regardless of provider.
    assert cfg.strong_model == "claude-sonnet-4-6"


def test_groq_cheap_tier_defaults_to_gpt_oss(monkeypatch: pytest.MonkeyPatch) -> None:
    # The GPT-OSS-on-Groq → Haiku recipe: only the providers + strong model are
    # set; the cheap model falls out of the provider default.
    monkeypatch.setenv("MESH_ROUTE_CHEAP_PROVIDER", "groq")
    monkeypatch.setenv("MESH_ROUTE_STRONG_PROVIDER", "anthropic")
    monkeypatch.setenv("MESH_ROUTE_STRONG_MODEL", "claude-haiku-4-5")
    cfg = RoutingConfig.from_env()
    assert cfg.cheap_provider == "groq"
    assert cfg.cheap_model == "openai/gpt-oss-120b"
    assert cfg.strong_provider == "anthropic"
    assert cfg.strong_model == "claude-haiku-4-5"


# ── round-trip of every knob ─────────────────────────────────────────────────


def test_from_env_round_trips_all_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_ROUTE_ENABLED", "true")
    monkeypatch.setenv("MESH_ROUTE_CHEAP_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("MESH_ROUTE_STRONG_MODEL", "claude-opus-4-8")
    monkeypatch.setenv("MESH_ROUTE_CHEAP_PROVIDER", "ollama")
    monkeypatch.setenv("MESH_ROUTE_STRONG_PROVIDER", "anthropic")
    monkeypatch.setenv("MESH_ROUTE_ESCALATE_CHARS", "5000")
    monkeypatch.setenv("MESH_ROUTE_ESCALATE_ON_PARSE_FAIL", "false")

    cfg = RoutingConfig.from_env()
    assert cfg.enabled is True
    assert cfg.cheap_model == "claude-haiku-4-5"
    assert cfg.strong_model == "claude-opus-4-8"
    assert cfg.cheap_provider == "ollama"
    assert cfg.strong_provider == "anthropic"
    assert cfg.escalate_chars == 5000
    assert cfg.escalate_on_parse_fail is False


def test_bad_escalate_chars_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_ROUTE_ESCALATE_CHARS", "not-a-number")
    assert RoutingConfig.from_env().escalate_chars == 12_000


# ── per-agent enable + strong override ───────────────────────────────────────


def test_per_agent_enable_overrides_global_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_ROUTE_ENABLED", "false")
    monkeypatch.setenv("MESH_ROUTE_SKEPTIC_ENABLED", "true")
    assert RoutingConfig.from_env("skeptic").enabled is True
    assert RoutingConfig.from_env("extraction").enabled is False


def test_per_agent_disable_overrides_global_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_ROUTE_ENABLED", "true")
    monkeypatch.setenv("MESH_ROUTE_EXTRACTION_ENABLED", "false")
    assert RoutingConfig.from_env("extraction").enabled is False
    assert RoutingConfig.from_env("skeptic").enabled is True


def test_per_agent_strong_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_ROUTE_STRONG_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("MESH_LLM_MODEL_SKEPTIC_STRONG", "claude-opus-4-8")
    assert RoutingConfig.from_env("skeptic").strong_model == "claude-opus-4-8"
    assert RoutingConfig.from_env("extraction").strong_model == "claude-sonnet-4-6"


# ── static-override bypass ───────────────────────────────────────────────────


def test_static_override_per_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_LLM_MODEL_SKEPTIC", "claude-opus-4-8")
    assert has_static_model_override("skeptic") is True
    assert has_static_model_override("extraction") is False


def test_static_override_global(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_LLM_MODEL", "claude-haiku-4-5")
    assert has_static_model_override("extraction") is True


def test_static_override_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_LLM_MODEL_DEFAULT", "claude-sonnet-4-6")
    assert has_static_model_override("skeptic") is True


def test_no_static_override_by_default() -> None:
    assert has_static_model_override("skeptic") is False
    assert has_static_model_override(None) is False


# ── classify_difficulty ──────────────────────────────────────────────────────


def test_classify_short_input_is_cheap() -> None:
    assert classify_difficulty("extract", "sys", "tiny abstract") is Tier.CHEAP


def test_classify_over_threshold_is_strong() -> None:
    cfg = RoutingConfig.from_env()
    big = "x" * (cfg.escalate_chars + 1)
    assert classify_difficulty("extract", "sys", big, config=cfg) is Tier.STRONG


def test_classify_at_threshold_is_strong() -> None:
    cfg = RoutingConfig.from_env()
    exact = "x" * cfg.escalate_chars
    assert classify_difficulty("extract", "sys", exact, config=cfg) is Tier.STRONG


def test_classify_route_hint_strong_forces_strong() -> None:
    assert (
        classify_difficulty("extract", "sys", "short", {"route_hint": "strong"})
        is Tier.STRONG
    )


def test_classify_route_hint_cheap_pins_cheap() -> None:
    cfg = RoutingConfig.from_env()
    big = "x" * (cfg.escalate_chars + 1)
    assert (
        classify_difficulty("extract", "sys", big, {"route_hint": "cheap"}, cfg)
        is Tier.CHEAP
    )

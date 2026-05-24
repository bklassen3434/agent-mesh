from __future__ import annotations

import pytest
from mesh_llm import AnthropicClient, OllamaClient, make_llm_client


def test_factory_defaults_to_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MESH_LLM_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    client = make_llm_client()
    assert isinstance(client, AnthropicClient)


def test_factory_selects_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_LLM_PROVIDER", "ollama")
    # OllamaClient construction touches the Ollama HTTP client but doesn't call
    # over the wire — fine to construct without an Ollama daemon running.
    client = make_llm_client()
    assert isinstance(client, OllamaClient)


def test_factory_unknown_provider_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_LLM_PROVIDER", "gpt5-fanfic")
    with pytest.raises(ValueError, match="Unknown MESH_LLM_PROVIDER"):
        make_llm_client()


def test_factory_provider_arg_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    client = make_llm_client(provider="anthropic")
    assert isinstance(client, AnthropicClient)


def test_anthropic_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from mesh_llm import AnthropicNotReadyError

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MESH_LLM_PROVIDER", "anthropic")
    with pytest.raises(AnthropicNotReadyError, match="ANTHROPIC_API_KEY"):
        make_llm_client()


# Per-agent model routing precedence ----------------------------------------


def _clear_model_env(mp: pytest.MonkeyPatch) -> None:
    for var in (
        "MESH_LLM_MODEL_SKEPTIC",
        "MESH_LLM_MODEL_EXTRACTION",
        "MESH_LLM_MODEL_DEFAULT",
        "MESH_LLM_MODEL",
    ):
        mp.delenv(var, raising=False)


def test_per_agent_env_wins_over_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("MESH_LLM_MODEL_DEFAULT", "claude-sonnet-4-6")
    monkeypatch.setenv("MESH_LLM_MODEL_SKEPTIC", "claude-opus-4-7")

    skeptic = make_llm_client(agent_name="skeptic")
    extractor = make_llm_client(agent_name="extraction")

    assert skeptic.model == "claude-opus-4-7"
    assert extractor.model == "claude-sonnet-4-6"  # no per-agent override → DEFAULT


def test_default_env_wins_over_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("MESH_LLM_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("MESH_LLM_MODEL_DEFAULT", "claude-sonnet-4-6")

    client = make_llm_client(agent_name="skeptic")
    assert client.model == "claude-sonnet-4-6"


def test_legacy_env_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 3 callers set only MESH_LLM_MODEL. Must remain functional."""
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("MESH_LLM_MODEL", "claude-haiku-4-5")

    client = make_llm_client(agent_name="extraction")
    assert client.model == "claude-haiku-4-5"


def test_provider_default_when_nothing_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    client = make_llm_client(agent_name="skeptic")
    # AnthropicClient's _DEFAULT_MODEL
    assert client.model == "claude-haiku-4-5"

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

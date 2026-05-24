from __future__ import annotations

from mesh_llm import (
    AnthropicNotReadyError,
    LLMProviderNotReadyError,
    OllamaNotReadyError,
)


def test_ollama_inherits_from_base() -> None:
    assert issubclass(OllamaNotReadyError, LLMProviderNotReadyError)


def test_anthropic_inherits_from_base() -> None:
    assert issubclass(AnthropicNotReadyError, LLMProviderNotReadyError)


def test_base_catches_both() -> None:
    for exc in (OllamaNotReadyError("a"), AnthropicNotReadyError("b")):
        try:
            raise exc
        except LLMProviderNotReadyError:
            pass
        else:
            raise AssertionError(f"{type(exc).__name__} not caught by base")

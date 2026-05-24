from __future__ import annotations

import os

from mesh_llm.anthropic_client import AnthropicClient
from mesh_llm.client import OllamaClient
from mesh_llm.protocol import LLMClient

_DEFAULT_PROVIDER = "anthropic"


def make_llm_client(provider: str | None = None) -> LLMClient:
    """Return the configured LLMClient implementation.

    Reads `MESH_LLM_PROVIDER` from the environment when `provider` is None.
    Defaults to Anthropic; pass "ollama" (or set the env var) to use the
    local Ollama path.
    """
    name = (provider or os.environ.get("MESH_LLM_PROVIDER") or _DEFAULT_PROVIDER).lower()
    if name == "anthropic":
        return AnthropicClient()
    if name == "ollama":
        return OllamaClient()
    raise ValueError(
        f"Unknown MESH_LLM_PROVIDER: {name!r}. Expected 'anthropic' or 'ollama'."
    )

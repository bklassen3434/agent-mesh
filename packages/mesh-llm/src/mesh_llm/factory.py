from __future__ import annotations

import os

from mesh_llm.anthropic_client import AnthropicClient
from mesh_llm.client import OllamaClient
from mesh_llm.protocol import LLMClient

_DEFAULT_PROVIDER = "anthropic"


def resolve_model(agent_name: str | None, provider_default: str) -> str:
    """Resolve which model to use for a given agent.

    Precedence (highest wins):
    1. `MESH_LLM_MODEL_<AGENT.upper()>` — per-agent override (e.g. MESH_LLM_MODEL_SKEPTIC)
    2. `MESH_LLM_MODEL_DEFAULT` — workspace-wide default override
    3. `MESH_LLM_MODEL` — back-compat with pre-Phase-4 single-model setups
    4. `provider_default` — the client class's hard-coded fallback

    Both AnthropicClient and OllamaClient call this from their constructors.
    """
    if agent_name:
        per_agent = os.environ.get(f"MESH_LLM_MODEL_{agent_name.upper()}")
        if per_agent:
            return per_agent
    return (
        os.environ.get("MESH_LLM_MODEL_DEFAULT")
        or os.environ.get("MESH_LLM_MODEL")
        or provider_default
    )


def make_llm_client(
    provider: str | None = None,
    agent_name: str | None = None,
) -> LLMClient:
    """Return the configured LLMClient implementation.

    `provider` (or `MESH_LLM_PROVIDER`) picks the backend (`anthropic` | `ollama`).
    `agent_name` selects which per-agent model env var to consult — see
    `resolve_model()` for the full precedence chain. Pass the agent's role
    (`"extraction"`, `"skeptic"`, `"sota"`, etc.); the constructor reads
    `MESH_LLM_MODEL_<AGENT>` from the environment.
    """
    name = (provider or os.environ.get("MESH_LLM_PROVIDER") or _DEFAULT_PROVIDER).lower()
    if name == "anthropic":
        return AnthropicClient(agent_name=agent_name)
    if name == "ollama":
        return OllamaClient(agent_name=agent_name)
    raise ValueError(
        f"Unknown MESH_LLM_PROVIDER: {name!r}. Expected 'anthropic' or 'ollama'."
    )

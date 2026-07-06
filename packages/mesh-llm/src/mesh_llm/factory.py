from __future__ import annotations

import os

from mesh_llm.anthropic_client import AnthropicClient
from mesh_llm.client import OllamaClient
from mesh_llm.groq_client import GroqClient
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

    `provider` (or `MESH_LLM_PROVIDER`) picks the backend
    (`anthropic` | `ollama` | `groq`).
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
    if name == "groq":
        return GroqClient(agent_name=agent_name)
    raise ValueError(
        f"Unknown MESH_LLM_PROVIDER: {name!r}. "
        "Expected 'anthropic', 'ollama', or 'groq'."
    )


def make_routed_llm_client(
    provider: str | None = None,
    agent_name: str | None = None,
) -> LLMClient:
    """Return a tier-routing client when routing is enabled, else a plain one.

    Additive companion to :func:`make_llm_client` (Phase 20). An agent opts in
    by switching its single construction line to this factory; nothing else
    changes. Routing is returned only when **both** hold:

    1. No static model pin exists for the agent
       (:func:`mesh_llm.routing.has_static_model_override`) — an explicit
       operator pin via ``MESH_LLM_MODEL_<AGENT>`` / ``MESH_LLM_MODEL_DEFAULT``
       / ``MESH_LLM_MODEL`` always wins and is never downgraded.
    2. Routing is enabled for the agent (``MESH_ROUTE_ENABLED`` /
       ``MESH_ROUTE_<AGENT>_ENABLED``).

    In every other case this delegates to :func:`make_llm_client`, so with
    routing off the behaviour is byte-for-byte today's.
    """
    from mesh_llm.routing import (
        RoutedLLMClient,
        RoutingConfig,
        has_static_model_override,
    )

    if has_static_model_override(agent_name):
        return make_llm_client(provider, agent_name)
    config = RoutingConfig.from_env(agent_name, provider_default=provider)
    if not config.enabled:
        return make_llm_client(provider, agent_name)
    return RoutedLLMClient(config, agent_name=agent_name)

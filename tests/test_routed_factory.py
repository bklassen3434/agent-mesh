"""Phase 20c — make_routed_llm_client opt-in factory."""
from __future__ import annotations

import pytest
from mesh_llm import AnthropicClient, make_routed_llm_client
from mesh_llm.routing import RoutedLLMClient

_VARS = (
    "MESH_ROUTE_ENABLED",
    "MESH_ROUTE_EXTRACTION_ENABLED",
    "MESH_ROUTE_SKEPTIC_ENABLED",
    "MESH_LLM_MODEL",
    "MESH_LLM_MODEL_DEFAULT",
    "MESH_LLM_MODEL_EXTRACTION",
)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    for v in _VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("MESH_LLM_PROVIDER", raising=False)


def test_routing_off_returns_plain_client(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_routed_llm_client(agent_name="extraction")
    assert isinstance(client, AnthropicClient)
    assert not isinstance(client, RoutedLLMClient)


def test_routing_on_returns_router(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_ROUTE_ENABLED", "true")
    client = make_routed_llm_client(agent_name="extraction")
    assert isinstance(client, RoutedLLMClient)


def test_per_agent_enable_returns_router(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_ROUTE_EXTRACTION_ENABLED", "true")
    assert isinstance(make_routed_llm_client(agent_name="extraction"), RoutedLLMClient)
    # A different agent without its own enable stays plain.
    assert not isinstance(
        make_routed_llm_client(agent_name="skeptic"), RoutedLLMClient
    )


def test_static_pin_bypasses_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even with routing enabled, an explicit operator pin wins.
    monkeypatch.setenv("MESH_ROUTE_ENABLED", "true")
    monkeypatch.setenv("MESH_LLM_MODEL_EXTRACTION", "claude-opus-4-8")
    client = make_routed_llm_client(agent_name="extraction")
    assert isinstance(client, AnthropicClient)
    assert not isinstance(client, RoutedLLMClient)
    assert client.model == "claude-opus-4-8"


def test_global_pin_bypasses_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESH_ROUTE_ENABLED", "true")
    monkeypatch.setenv("MESH_LLM_MODEL", "claude-haiku-4-5")
    assert not isinstance(
        make_routed_llm_client(agent_name="extraction"), RoutedLLMClient
    )

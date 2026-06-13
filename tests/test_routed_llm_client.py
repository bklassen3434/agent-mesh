"""Phase 20b — RoutedLLMClient routing behaviour with mock tier clients."""
from __future__ import annotations

from typing import Any

import pytest
from mesh_llm import LLMClient, LLMResponseError
from mesh_llm.routing import RoutedLLMClient, RoutingConfig, Tier
from mesh_llm.usage import LLMUsage


class _FakeClient:
    """Records the calls it receives and returns canned latency/usage.

    Optionally raises ``LLMResponseError`` on its first call to exercise the
    cheap→strong parse-failure escalation.
    """

    def __init__(self, model: str, *, fail_once: bool = False) -> None:
        self.model = model
        self.agent_name: str | None = None
        self._fail_once = fail_once
        self.calls: list[dict[str, Any]] = []
        self.health_checked = False

    def health_check(self) -> None:
        self.health_checked = True

    def complete_with_latency(
        self,
        name: str,
        system: str,
        user: str,
        response_model: Any = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[Any, int]:
        self.calls.append({"options": options})
        if self._fail_once and len(self.calls) == 1:
            raise LLMResponseError(f"parse fail on {self.model}")
        return f"answer:{self.model}", 42

    def complete_with_usage(
        self,
        name: str,
        system: str,
        user: str,
        response_model: Any = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[Any, int, LLMUsage]:
        self.calls.append({"options": options})
        if self._fail_once and len(self.calls) == 1:
            raise LLMResponseError(f"parse fail on {self.model}")
        return f"answer:{self.model}", 42, LLMUsage(input_tokens=10, output_tokens=5)


def _routed(
    monkeypatch: pytest.MonkeyPatch,
    cheap: _FakeClient,
    strong: _FakeClient,
    *,
    escalate_chars: int = 12_000,
    escalate_on_parse_fail: bool = True,
) -> RoutedLLMClient:
    cfg = RoutingConfig(
        enabled=True,
        cheap_model=cheap.model,
        strong_model=strong.model,
        cheap_provider="anthropic",
        strong_provider="anthropic",
        escalate_chars=escalate_chars,
        escalate_on_parse_fail=escalate_on_parse_fail,
    )
    router = RoutedLLMClient(cfg, agent_name="extraction")
    # Inject fakes so no real provider SDK is constructed.
    router._cheap = cheap
    router._strong = strong
    return router


def test_is_llmclient_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _routed(monkeypatch, _FakeClient("haiku"), _FakeClient("sonnet"))
    assert isinstance(router, LLMClient)
    # model reports the cheap tier (the default path).
    assert router.model == "haiku"


def test_under_threshold_goes_cheap(monkeypatch: pytest.MonkeyPatch) -> None:
    cheap, strong = _FakeClient("haiku"), _FakeClient("sonnet")
    router = _routed(monkeypatch, cheap, strong)
    result, latency, usage = router.complete_with_usage("x", "sys", "short input")
    assert result == "answer:haiku"
    assert latency == 42
    assert usage.input_tokens == 10
    assert len(cheap.calls) == 1
    assert strong.calls == []


def test_over_threshold_goes_strong(monkeypatch: pytest.MonkeyPatch) -> None:
    cheap, strong = _FakeClient("haiku"), _FakeClient("sonnet")
    router = _routed(monkeypatch, cheap, strong, escalate_chars=100)
    result, _ = router.complete_with_latency("x", "sys", "y" * 200)
    assert result == "answer:sonnet"
    assert cheap.calls == []
    assert len(strong.calls) == 1


def test_route_hint_strong_forces_strong(monkeypatch: pytest.MonkeyPatch) -> None:
    cheap, strong = _FakeClient("haiku"), _FakeClient("sonnet")
    router = _routed(monkeypatch, cheap, strong)
    result, _, _ = router.complete_with_usage(
        "x", "sys", "short", options={"route_hint": "strong"}
    )
    assert result == "answer:sonnet"
    assert len(strong.calls) == 1


def test_cheap_parse_fail_escalates_to_strong(monkeypatch: pytest.MonkeyPatch) -> None:
    cheap = _FakeClient("haiku", fail_once=True)
    strong = _FakeClient("sonnet")
    router = _routed(monkeypatch, cheap, strong)
    result, _, _ = router.complete_with_usage("x", "sys", "short input")
    assert result == "answer:sonnet"
    assert len(cheap.calls) == 1  # tried cheap, it raised
    assert len(strong.calls) == 1  # then retried on strong


def test_parse_fail_no_escalation_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    cheap = _FakeClient("haiku", fail_once=True)
    strong = _FakeClient("sonnet")
    router = _routed(monkeypatch, cheap, strong, escalate_on_parse_fail=False)
    with pytest.raises(LLMResponseError):
        router.complete_with_usage("x", "sys", "short input")
    assert strong.calls == []


def test_strong_parse_fail_does_not_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    # An over-threshold request lands on strong; if strong itself parse-fails it
    # must surface, not retry forever.
    cheap = _FakeClient("haiku")
    strong = _FakeClient("sonnet", fail_once=True)
    router = _routed(monkeypatch, cheap, strong, escalate_chars=10)
    with pytest.raises(LLMResponseError):
        router.complete_with_usage("x", "sys", "y" * 50)
    assert len(strong.calls) == 1
    assert cheap.calls == []


def test_health_check_only_touches_cheap(monkeypatch: pytest.MonkeyPatch) -> None:
    cheap, strong = _FakeClient("haiku"), _FakeClient("sonnet")
    router = _routed(monkeypatch, cheap, strong)
    router.health_check()
    assert cheap.health_checked is True
    assert strong.health_checked is False


def test_trace_metadata_passed_in_options(monkeypatch: pytest.MonkeyPatch) -> None:
    cheap, strong = _FakeClient("haiku"), _FakeClient("sonnet")
    router = _routed(monkeypatch, cheap, strong)
    router.complete_with_usage("x", "sys", "short")
    meta = cheap.calls[0]["options"]["_route"]
    assert meta["routed"] is True
    assert meta["tier"] == "cheap"
    assert "route_reason" in meta


def test_decide_returns_explainable_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    router = _routed(monkeypatch, _FakeClient("haiku"), _FakeClient("sonnet"))
    cheap_decision = router.decide("x", "sys", "short")
    assert cheap_decision.tier is Tier.CHEAP
    assert cheap_decision.model == "haiku"
    assert "cheap" in cheap_decision.reason.lower()

"""Phase 20b — RoutedLLMClient routing behaviour with mock tier clients."""
from __future__ import annotations

from typing import Any

import pytest
from mesh_llm import LLMClient, LLMRateLimitedError, LLMResponseError
from mesh_llm.routing import RoutedLLMClient, RoutingConfig, Tier
from mesh_llm.usage import LLMUsage


class _FakeClient:
    """Records the calls it receives and returns canned latency/usage.

    Optionally raises ``LLMResponseError`` (or ``LLMRateLimitedError``) on its
    first call to exercise the cheap→strong escalations.
    """

    def __init__(
        self, model: str, *, fail_once: bool = False, rate_limit_once: bool = False
    ) -> None:
        self.model = model
        self.agent_name: str | None = None
        self._fail_once = fail_once
        self._rate_limit_once = rate_limit_once
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
        if self._rate_limit_once and len(self.calls) == 1:
            raise LLMRateLimitedError(f"rate limited on {self.model}")
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
        if self._rate_limit_once and len(self.calls) == 1:
            raise LLMRateLimitedError(f"rate limited on {self.model}")
        # Real clients stamp the model that served the call onto the usage.
        return (
            f"answer:{self.model}",
            42,
            LLMUsage(model=self.model, input_tokens=10, output_tokens=5),
        )


def _routed(
    monkeypatch: pytest.MonkeyPatch,
    cheap: _FakeClient,
    strong: _FakeClient,
    *,
    escalate_chars: int = 12_000,
    escalate_on_parse_fail: bool = True,
    escalate_on_rate_limit: bool = True,
) -> RoutedLLMClient:
    cfg = RoutingConfig(
        enabled=True,
        cheap_model=cheap.model,
        strong_model=strong.model,
        cheap_provider="anthropic",
        strong_provider="anthropic",
        escalate_chars=escalate_chars,
        escalate_on_parse_fail=escalate_on_parse_fail,
        escalate_on_rate_limit=escalate_on_rate_limit,
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


def test_usage_carries_realized_model_on_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The returned usage must report the model that actually served the call,
    so cost ledgering attributes an escalated request to the strong tier (not
    the cheap-tier ``router.model``)."""
    # Length-based escalation.
    cheap, strong = _FakeClient("haiku"), _FakeClient("sonnet")
    router = _routed(monkeypatch, cheap, strong, escalate_chars=100)
    _, _, usage = router.complete_with_usage("x", "sys", "y" * 200)
    assert usage.model == "sonnet"
    assert router.model == "haiku"  # the Protocol attribute is unchanged

    # Parse-failure escalation.
    cheap2 = _FakeClient("haiku", fail_once=True)
    strong2 = _FakeClient("sonnet")
    router2 = _routed(monkeypatch, cheap2, strong2)
    _, _, usage2 = router2.complete_with_usage("x", "sys", "short input")
    assert usage2.model == "sonnet"

    # The cheap default path reports the cheap model.
    cheap3, strong3 = _FakeClient("haiku"), _FakeClient("sonnet")
    router3 = _routed(monkeypatch, cheap3, strong3)
    _, _, usage3 = router3.complete_with_usage("x", "sys", "short input")
    assert usage3.model == "haiku"


def test_cheap_rate_limit_escalates_to_strong(monkeypatch: pytest.MonkeyPatch) -> None:
    cheap = _FakeClient("gpt-oss", rate_limit_once=True)
    strong = _FakeClient("haiku")
    router = _routed(monkeypatch, cheap, strong)
    result, _, usage = router.complete_with_usage("x", "sys", "short input")
    assert result == "answer:haiku"
    assert usage.model == "haiku"
    assert len(cheap.calls) == 1
    assert len(strong.calls) == 1
    meta = strong.calls[0]["options"]["_route"]
    assert meta["route_reason"] == "cheap tier rate-limited → escalate"


def test_rate_limit_no_escalation_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    cheap = _FakeClient("gpt-oss", rate_limit_once=True)
    strong = _FakeClient("haiku")
    router = _routed(monkeypatch, cheap, strong, escalate_on_rate_limit=False)
    with pytest.raises(LLMRateLimitedError):
        router.complete_with_usage("x", "sys", "short input")
    assert strong.calls == []


def test_strong_rate_limit_does_not_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    cheap = _FakeClient("gpt-oss")
    strong = _FakeClient("haiku", rate_limit_once=True)
    router = _routed(monkeypatch, cheap, strong, escalate_chars=10)
    with pytest.raises(LLMRateLimitedError):
        router.complete_with_usage("x", "sys", "y" * 50)
    assert len(strong.calls) == 1
    assert cheap.calls == []


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


def test_build_tier_client_supports_groq(monkeypatch: pytest.MonkeyPatch) -> None:
    from mesh_llm import GroqClient
    from mesh_llm.routing import _build_tier_client

    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    client = _build_tier_client("groq", "openai/gpt-oss-120b", "extraction")
    assert isinstance(client, GroqClient)
    assert client.model == "openai/gpt-oss-120b"
    assert client.agent_name == "extraction"

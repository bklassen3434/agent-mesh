"""Tiered model routing (Phase 20).

A *router* sends the bulk of LLM traffic to a cheap tier and **escalates** the
hard or failed cases to a strong tier — without changing any agent's logic and
without disturbing the existing static-override path in
:mod:`mesh_llm.factory`.

This module is purely additive. It does **not** touch ``make_llm_client`` or
``resolve_model``. With routing disabled (the default), the new factory
``make_routed_llm_client`` returns a plain client identical to today's, so no
nondeterministic model choice ever reaches a test without explicit config.

Layering:

- block 20a (this commit) — the *policy*: :class:`Tier`, :class:`RoutingConfig`
  (env), :func:`classify_difficulty` (pure, LLM-free), :class:`RoutingDecision`,
  and the static-override bypass (:func:`has_static_model_override`). No client
  wiring yet.
- block 20b — :class:`RoutedLLMClient`, the Protocol-conforming wrapper.

Precedence an operator must understand:

1. **Static pin wins.** If ``MESH_LLM_MODEL_<AGENT>`` / ``MESH_LLM_MODEL_DEFAULT``
   / ``MESH_LLM_MODEL`` pins a model, routing is bypassed entirely for that
   agent (see :func:`has_static_model_override`). An explicit operator pin is
   never silently downgraded to the cheap tier.
2. Otherwise, if routing is enabled for the agent, the request is classified
   cheap-first and escalated only on a difficulty signal or a parse failure.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from mesh_llm.client import LLMResponseError
from mesh_llm.protocol import LLMClient

# Tier defaults. The cheap tier mirrors each provider's current hard default so
# "routing on, no other config" behaves like today's single-model setup for the
# common path and only escalates the genuinely hard cases.
_DEFAULT_CHEAP_MODEL: dict[str, str] = {
    "anthropic": "claude-haiku-4-5",
    "ollama": "qwen3:8b",
    "groq": "openai/gpt-oss-120b",
}
_DEFAULT_STRONG_MODEL = "claude-sonnet-4-6"
_DEFAULT_PROVIDER = "anthropic"

# Tuned to a long paper / dense multi-benchmark table: under this the cheap tier
# handles the request, over it we escalate. Roughly ~3k tokens of user content.
_DEFAULT_ESCALATE_CHARS = 12_000

# Reserved options key the router uses to pass its decision down to the
# underlying client's tracing (consumed in 20b). Namespaced so it never collides
# with a real provider option and is always stripped before hitting the wire.
ROUTE_OPTION_KEY = "_route"

_TRUTHY = {"1", "true", "yes", "on"}


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


class Tier(StrEnum):
    """Which model tier handles a request. ``StrEnum`` so the value is its own
    serialized form (``Tier.CHEAP.value == "cheap"``)."""

    CHEAP = "cheap"
    STRONG = "strong"


@dataclass(frozen=True)
class RoutingDecision:
    """The outcome of routing a single request — the object traced + costed.

    ``reason`` is a short, human-readable explanation ("user content
    12345 chars ≥ 12000", "route_hint=strong", "cheap parse failure → escalate",
    "default cheap") so every escalation is explainable after the fact.
    """

    tier: Tier
    model: str
    provider: str
    reason: str


@dataclass(frozen=True)
class RoutingConfig:
    """Resolved routing knobs for one agent, read from the environment.

    Construct with :meth:`from_env`. ``enabled`` already folds the global flag
    and the per-agent override together, so the factory only has to check this
    one field.
    """

    enabled: bool
    cheap_model: str
    strong_model: str
    cheap_provider: str
    strong_provider: str
    escalate_chars: int
    escalate_on_parse_fail: bool

    @classmethod
    def from_env(
        cls,
        agent_name: str | None = None,
        *,
        provider_default: str | None = None,
    ) -> RoutingConfig:
        """Build config from the ``MESH_ROUTE_*`` env vars.

        ``provider_default`` (or ``MESH_LLM_PROVIDER``) sets the base provider for
        both tiers unless ``MESH_ROUTE_CHEAP_PROVIDER`` /
        ``MESH_ROUTE_STRONG_PROVIDER`` override one of them (e.g. cheap=local
        Ollama, strong=Anthropic API).

        ``enabled`` is the global ``MESH_ROUTE_ENABLED`` (default ``false``),
        overridden per agent by ``MESH_ROUTE_<AGENT>_ENABLED`` when that var is
        set — so the skeptic can route while the personalizer does not, or vice
        versa.
        """
        base_provider = (
            provider_default
            or os.environ.get("MESH_LLM_PROVIDER")
            or _DEFAULT_PROVIDER
        ).lower()

        enabled = _env_flag("MESH_ROUTE_ENABLED", False)
        if agent_name:
            per_agent = os.environ.get(f"MESH_ROUTE_{agent_name.upper()}_ENABLED")
            if per_agent is not None:
                enabled = per_agent.strip().lower() in _TRUTHY

        cheap_provider = (
            os.environ.get("MESH_ROUTE_CHEAP_PROVIDER") or base_provider
        ).lower()
        strong_provider = (
            os.environ.get("MESH_ROUTE_STRONG_PROVIDER") or base_provider
        ).lower()

        cheap_model = os.environ.get("MESH_ROUTE_CHEAP_MODEL") or _DEFAULT_CHEAP_MODEL.get(
            cheap_provider, _DEFAULT_CHEAP_MODEL["anthropic"]
        )

        # Per-agent strong-model override lets extraction escalate only to Sonnet
        # while the skeptic escalates to Opus.
        strong_model = (
            (
                agent_name
                and os.environ.get(f"MESH_LLM_MODEL_{agent_name.upper()}_STRONG")
            )
            or os.environ.get("MESH_ROUTE_STRONG_MODEL")
            or _DEFAULT_STRONG_MODEL
        )

        return cls(
            enabled=enabled,
            cheap_model=cheap_model,
            strong_model=strong_model,
            cheap_provider=cheap_provider,
            strong_provider=strong_provider,
            escalate_chars=_int_env("MESH_ROUTE_ESCALATE_CHARS", _DEFAULT_ESCALATE_CHARS),
            escalate_on_parse_fail=_env_flag("MESH_ROUTE_ESCALATE_ON_PARSE_FAIL", True),
        )


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def has_static_model_override(agent_name: str | None) -> bool:
    """True when an operator has pinned a concrete model for this agent.

    Mirrors :func:`mesh_llm.factory.resolve_model`'s precedence: a per-agent
    ``MESH_LLM_MODEL_<AGENT>``, the workspace-wide ``MESH_LLM_MODEL_DEFAULT``, or
    the legacy ``MESH_LLM_MODEL`` each count as an explicit pin. When any is set,
    routing is bypassed for that agent — the pin wins and is never downgraded.
    """
    if agent_name and os.environ.get(f"MESH_LLM_MODEL_{agent_name.upper()}"):
        return True
    return bool(
        os.environ.get("MESH_LLM_MODEL_DEFAULT") or os.environ.get("MESH_LLM_MODEL")
    )


def _difficulty(
    name: str,
    system: str,
    user: str,
    options: dict[str, Any] | None,
    config: RoutingConfig,
) -> tuple[Tier, str]:
    """Pure, LLM-free difficulty classification returning (tier, reason).

    The rule set is intentionally small and explainable — request
    classification must not itself call a model on the hot path (that would
    defeat the savings). Escalate to ``strong`` when:

    - ``options["route_hint"] == "strong"`` (an agent or caller marks this
      specific request hard), or
    - the user content length reaches ``config.escalate_chars``.

    Otherwise ``cheap``.
    """
    hint = (options or {}).get("route_hint")
    if hint == "strong":
        return Tier.STRONG, "route_hint=strong"
    if hint == "cheap":
        # An explicit cheap hint pins the cheap tier even for long inputs.
        return Tier.CHEAP, "route_hint=cheap"

    user_len = len(user or "")
    if user_len >= config.escalate_chars:
        return Tier.STRONG, f"user content {user_len} chars ≥ {config.escalate_chars}"

    return Tier.CHEAP, f"default cheap (user content {user_len} chars)"


def classify_difficulty(
    name: str,
    system: str,
    user: str,
    options: dict[str, Any] | None = None,
    config: RoutingConfig | None = None,
) -> Tier:
    """Public, pure difficulty classifier → :class:`Tier`.

    LLM-free by contract. ``config`` supplies the escalation threshold; when
    omitted, a default-from-env config is used so the function is callable
    standalone (e.g. in tests).
    """
    cfg = config or RoutingConfig.from_env()
    return _difficulty(name, system, user, options, cfg)[0]


# ── RoutedLLMClient (block 20b) ──────────────────────────────────────────────


def _build_tier_client(
    provider: str, model: str, agent_name: str | None
) -> LLMClient:
    """Construct a concrete client for a tier, pinning its model explicitly.

    Passing ``model=`` bypasses ``resolve_model`` — the router, not the env
    precedence chain, owns the tier's model. Imported lazily to keep this module
    importable without instantiating any provider SDK.
    """
    from mesh_llm.anthropic_client import AnthropicClient
    from mesh_llm.client import OllamaClient
    from mesh_llm.groq_client import GroqClient

    name = provider.lower()
    if name == "anthropic":
        return AnthropicClient(model=model, agent_name=agent_name)
    if name == "ollama":
        return OllamaClient(model=model, agent_name=agent_name)
    if name == "groq":
        return GroqClient(model=model, agent_name=agent_name)
    raise ValueError(
        f"Unknown routing provider: {name!r}. "
        "Expected 'anthropic', 'ollama', or 'groq'."
    )


class RoutedLLMClient:
    """An :class:`~mesh_llm.protocol.LLMClient` that routes each request to a
    cheap or strong tier.

    The wrapper *is* an ``LLMClient`` (same Protocol surface), so agents, the
    batch path, and tracing treat it like any other client — no agent learns it
    is being routed. Per request it calls :func:`classify_difficulty`; the
    default path is the cheap tier and escalation is the exception. A cheap-tier
    ``LLMResponseError`` (parse failure) triggers one retry on the strong tier
    (``MESH_ROUTE_ESCALATE_ON_PARSE_FAIL``, default on). Provider-not-ready
    errors propagate — routing never swallows an unconfigured-provider error.

    ``model`` (the Protocol attribute) reports the cheap-tier model, since that
    is the default path. The tier clients are built lazily so a strong-tier-only
    misconfiguration an enabled run may never hit does not fail construction.
    """

    def __init__(self, config: RoutingConfig, agent_name: str | None = None) -> None:
        self._config = config
        self.agent_name = agent_name
        self.model = config.cheap_model
        self._cheap: LLMClient | None = None
        self._strong: LLMClient | None = None

    # -- tier client accessors ------------------------------------------------

    @property
    def cheap_client(self) -> LLMClient:
        if self._cheap is None:
            self._cheap = _build_tier_client(
                self._config.cheap_provider, self._config.cheap_model, self.agent_name
            )
        return self._cheap

    @property
    def strong_client(self) -> LLMClient:
        """The strong-tier client. Exposed so a batch caller (skeptic sweep /
        consolidation) can opt a whole batch into the strong tier — per-item
        batch routing is out of scope for this phase."""
        if self._strong is None:
            self._strong = _build_tier_client(
                self._config.strong_provider, self._config.strong_model, self.agent_name
            )
        return self._strong

    def _client_for(self, tier: Tier) -> LLMClient:
        return self.strong_client if tier is Tier.STRONG else self.cheap_client

    # -- routing decision -----------------------------------------------------

    def decide(
        self,
        name: str,
        system: str,
        user: str,
        options: dict[str, Any] | None = None,
    ) -> RoutingDecision:
        """Classify one request into a :class:`RoutingDecision` (tier + model +
        provider + reason) without calling any model."""
        tier, reason = _difficulty(name, system, user, options, self._config)
        if tier is Tier.STRONG:
            return RoutingDecision(
                Tier.STRONG,
                self._config.strong_model,
                self._config.strong_provider,
                reason,
            )
        return RoutingDecision(
            Tier.CHEAP, self._config.cheap_model, self._config.cheap_provider, reason
        )

    def _with_trace_meta(
        self, options: dict[str, Any] | None, decision: RoutingDecision
    ) -> dict[str, Any]:
        """Attach the decision (tier + reason) to the call options so the
        underlying client forwards it to ``trace_generation`` metadata."""
        return {
            **(options or {}),
            ROUTE_OPTION_KEY: {
                "routed": True,
                "tier": decision.tier.value,
                "route_reason": decision.reason,
            },
        }

    # -- LLMClient Protocol ---------------------------------------------------

    def health_check(self) -> None:
        """Check only the cheap tier — the default path every enabled run
        exercises. The strong tier is checked lazily on first escalation, so a
        strong-only misconfiguration a run never reaches does not hard-fail
        startup."""
        self.cheap_client.health_check()

    def complete_with_latency(
        self,
        name: str,
        system: str,
        user: str,
        response_model: Any = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[Any, int]:
        decision = self.decide(name, system, user, options)
        try:
            return self._client_for(decision.tier).complete_with_latency(
                name, system, user, response_model,
                self._with_trace_meta(options, decision),
            )
        except LLMResponseError:
            escalation = self._escalate_after_parse_fail(decision)
            if escalation is None:
                raise
            return self.strong_client.complete_with_latency(
                name, system, user, response_model,
                self._with_trace_meta(options, escalation),
            )

    def complete_with_usage(
        self,
        name: str,
        system: str,
        user: str,
        response_model: Any = None,
        options: dict[str, Any] | None = None,
    ) -> tuple[Any, int, Any]:
        decision = self.decide(name, system, user, options)
        try:
            return self._client_for(decision.tier).complete_with_usage(
                name, system, user, response_model,
                self._with_trace_meta(options, decision),
            )
        except LLMResponseError:
            escalation = self._escalate_after_parse_fail(decision)
            if escalation is None:
                raise
            return self.strong_client.complete_with_usage(
                name, system, user, response_model,
                self._with_trace_meta(options, escalation),
            )

    def _escalate_after_parse_fail(
        self, decision: RoutingDecision
    ) -> RoutingDecision | None:
        """Decide whether a cheap-tier parse failure should retry on strong.

        Returns the strong-tier decision to retry with, or ``None`` to re-raise
        (already on strong, or escalate-on-parse-fail disabled)."""
        if decision.tier is Tier.STRONG or not self._config.escalate_on_parse_fail:
            return None
        return RoutingDecision(
            Tier.STRONG,
            self._config.strong_model,
            self._config.strong_provider,
            "cheap parse failure → escalate",
        )

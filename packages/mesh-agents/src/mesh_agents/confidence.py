"""Evidence-derived belief confidence (Phase 14d).

Replaces the hardcoded ``0.5`` every synthesized belief used to carry with a
value computed from the belief's own signals — the same inputs that drive the
``belief_hype_substance`` view: source-type diversity, reproduction count, and
skeptic attacks (count + severe failure modes). The coordinator reads those
signals from the view after a belief's claim links exist, then calls in here.

The weights live in config (env), not buried in code, so a later calibration
phase can tune them without a rewrite. The defaults are hand-set to reproduce
the ``belief_hype_substance`` formula exactly (base 0.5, equal support/attack
weight 0.5, the same /4 and /3 saturation caps); this phase does NOT fit them
against outcomes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class BeliefSignals:
    """The evidence signals behind a belief's confidence (mirrors the
    ``belief_signals`` view columns)."""

    source_type_diversity: int = 0
    reproduction_count: int = 0
    skeptic_counter_claim_count: int = 0
    severe_failure_mode_count: int = 0

    @classmethod
    def from_row(cls, row: dict[str, int]) -> BeliefSignals:
        return cls(
            source_type_diversity=int(row.get("source_type_diversity", 0)),
            reproduction_count=int(row.get("reproduction_count", 0)),
            skeptic_counter_claim_count=int(row.get("skeptic_counter_claim_count", 0)),
            severe_failure_mode_count=int(row.get("severe_failure_mode_count", 0)),
        )


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class ConfidenceWeights:
    """Config-tunable weights for evidence→confidence. Defaults reproduce the
    belief_hype_substance formula."""

    base: float = 0.5
    support_weight: float = 0.5
    attack_weight: float = 0.5
    source_diversity_cap: float = 4.0
    reproduction_cap: float = 3.0
    skeptic_cap: float = 4.0
    severe_cap: float = 3.0

    @classmethod
    def from_env(cls) -> ConfidenceWeights:
        return cls(
            base=_env_float("MESH_CONFIDENCE_BASE", 0.5),
            support_weight=_env_float("MESH_CONFIDENCE_SUPPORT_WEIGHT", 0.5),
            attack_weight=_env_float("MESH_CONFIDENCE_ATTACK_WEIGHT", 0.5),
            source_diversity_cap=_env_float("MESH_CONFIDENCE_SOURCE_DIVERSITY_CAP", 4.0),
            reproduction_cap=_env_float("MESH_CONFIDENCE_REPRODUCTION_CAP", 3.0),
            skeptic_cap=_env_float("MESH_CONFIDENCE_SKEPTIC_CAP", 4.0),
            severe_cap=_env_float("MESH_CONFIDENCE_SEVERE_CAP", 3.0),
        )


def _saturate(value: float, cap: float) -> float:
    if cap <= 0:
        return 0.0
    return min(value / cap, 1.0)


def compute_confidence(
    signals: BeliefSignals, weights: ConfidenceWeights | None = None
) -> float:
    """Map evidence signals to a confidence in [0, 1].

    Support (source diversity + reproduction) lifts confidence above the base;
    attacks (skeptic counter-claims + severe failure modes) pull it down. Each
    term saturates at its cap so a single very-noisy signal can't dominate.
    """
    w = weights or ConfidenceWeights()
    support = (
        _saturate(signals.source_type_diversity, w.source_diversity_cap)
        + _saturate(signals.reproduction_count, w.reproduction_cap)
    ) / 2.0
    attack = (
        _saturate(signals.skeptic_counter_claim_count, w.skeptic_cap)
        + _saturate(signals.severe_failure_mode_count, w.severe_cap)
    ) / 2.0
    score = w.base + w.support_weight * support - w.attack_weight * attack
    return max(0.0, min(1.0, score))

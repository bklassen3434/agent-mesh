"""Entity-resolution actuator — auto-tune the merge thresholds (config kind).

The second config-kind actuator (after ``confidence``). An ``entity_resolution``
concern means a belief conflates two different entities (or split one): a
**calibration** fault in the similarity bands that decide auto-merge vs
auto-reject vs LLM-adjudicate (``classify_pair`` / ``ResolutionConfig``). Live faults
are almost always over-merging (a false merge corrupts provenance), so ``draft``
nudges the auto-merge band **up** — more conservative, fewer cheap auto-merges,
more borderline pairs deferred to the adjudicator.

It shadow-tests against the **LLM adjudicator as the oracle**: pull real
near-duplicate candidate pairs, get the oracle's same/different verdict per pair,
then score how well each threshold config's *band decision* agrees with it:

- ``merge``      auto-decides "same"      → correct iff the oracle says same
- ``reject``     auto-decides "different" → correct iff the oracle says different
- ``adjudicate`` defers to the oracle     → always correct (it asks the oracle)

So raising ``high`` can only turn a *false* auto-merge into a (correct) deferral —
treatment beats control exactly when the current band auto-merges pairs the oracle
rejects, and ties it (so the margin gate rejects a pointless tightening) when the
auto-merges were all sound. Accuracy is the objective; the extra adjudication cost a
tighter band incurs is not scored here. Promotion writes the winning thresholds to
the per-field config the live ``ResolutionConfig.resolve`` overlays.
"""
from __future__ import annotations

import os
from typing import Any

from mesh_models.improvement_concern import ImprovementConcern
from mesh_models.improvement_experiment import ExperimentArm, ImprovementExperiment

from mesh_agents.actuators.base import register_actuator

# Min real candidate pairs before a threshold comparison means anything.
_MIN_PAIRS = 4
# How hard one draft nudges the auto-merge band up (capped just below 1.0 so a
# genuinely identical pair can still auto-merge).
_HIGH_STEP = 0.02
_HIGH_CAP = 0.99


@register_actuator
class EntityResolutionActuator:
    component = "entity_resolution"
    target = "merge-candidate"
    uses_llm = True  # shadow_sample labels pairs with the LLM oracle

    # -- draft ---------------------------------------------------------------

    def draft(
        self, conn: Any, field_id: str, concerns: list[ImprovementConcern]
    ) -> dict[str, Any] | None:
        from mesh_agents.entity_resolution import ResolutionConfig

        cfg = ResolutionConfig.resolve(conn, field_id)
        candidate = min(_HIGH_CAP, round(cfg.high + _HIGH_STEP, 4))
        if candidate <= cfg.high:
            return None  # already at the conservative ceiling — nothing to try
        # Only fire if there are enough real candidate pairs to judge the change.
        if len(self._candidate_pairs(conn, field_id, cfg)) < _MIN_PAIRS:
            return None
        return {"config": {"entity_resolution.high": candidate}}

    # -- shadow --------------------------------------------------------------

    def shadow_sample(
        self, conn: Any, field_id: str, exp: ImprovementExperiment
    ) -> list[tuple[ExperimentArm, float]]:
        from mesh_agents.entity_resolution import (
            ResolutionConfig,
            classify_pair,
        )

        control = ResolutionConfig.resolve(conn, field_id)
        treatment = control.overlay(exp.treatment.get("config", {}))
        pairs = self._candidate_pairs(conn, field_id, control, treatment)
        llm = self._llm()
        if not pairs or llm is None:
            return []

        out: list[tuple[ExperimentArm, float]] = []
        for id_a, id_b, similarity in pairs:
            oracle_same = self._oracle_same(conn, field_id, id_a, id_b, llm)
            if oracle_same is None:
                continue  # couldn't get an oracle label (missing entity / parse) — skip
            for arm, cfg in (
                (ExperimentArm.control, control),
                (ExperimentArm.treatment, treatment),
            ):
                decision = classify_pair(similarity, cfg)
                out.append((arm, _agreement(decision, oracle_same)))
        return out

    # -- promote -------------------------------------------------------------

    def promote_effects(self, exp: ImprovementExperiment) -> list[Any]:
        from mesh_models.effect import SetFieldConfigEffect

        config = {k: float(v) for k, v in exp.treatment.get("config", {}).items()}
        c, t = exp.control_score or 0.0, exp.treatment_score or 0.0
        return [
            SetFieldConfigEffect(
                field_id=exp.field_id, values=config,
                rationale=f"shadow A/B promoted — band agreement {c:.3f} → {t:.3f}",
            )
        ]

    # -- helpers -------------------------------------------------------------

    def _candidate_pairs(
        self, conn: Any, field_id: str, *cfgs: Any
    ) -> list[tuple[str, str, float]]:
        """Real near-duplicate pairs in the decision-relevant range (at/above the
        lowest ``low`` band any arm uses, so every pair a threshold move could
        reclassify is covered), capped to one shadow batch."""
        from mesh_db.entities import find_duplicate_candidate_pairs

        low = min((c.low for c in cfgs), default=0.80)
        k = max(1, int(os.environ.get("MESH_EXPERIMENT_SAMPLE_BATCH", "5")))
        try:
            rows = find_duplicate_candidate_pairs(
                conn, field_id=field_id, min_similarity=low, limit=k
            )
        except Exception:
            return []
        return [(a, b, sim) for (a, _na, b, _nb, sim) in rows]

    def _oracle_same(
        self, conn: Any, field_id: str, id_a: str, id_b: str, llm: Any
    ) -> bool | None:
        from mesh_db.claims import list_claims
        from mesh_db.entities import get_entity_by_id

        from mesh_agents.entity_resolution import (
            adjudicate_same_entity,
            entity_for_match_from_claims,
        )

        ent_a = get_entity_by_id(conn, id_a)
        ent_b = get_entity_by_id(conn, id_b)
        if ent_a is None or ent_b is None:
            return None
        a = entity_for_match_from_claims(
            ent_a.canonical_name, ent_a.type, aliases=list(ent_a.aliases),
            claims=list_claims(conn, entity_id=id_a, limit=3, field_id=field_id),
        )
        b = entity_for_match_from_claims(
            ent_b.canonical_name, ent_b.type, aliases=list(ent_b.aliases),
            claims=list_claims(conn, entity_id=id_b, limit=3, field_id=field_id),
        )
        try:
            return adjudicate_same_entity(llm, a, b).same_entity
        except Exception:
            return None

    def _llm(self) -> Any | None:
        try:
            from mesh_llm import make_routed_llm_client

            llm = make_routed_llm_client(agent_name="entity_resolution")
            llm.health_check()
            return llm
        except Exception:
            try:
                from mesh_llm import make_llm_client

                return make_llm_client()
            except Exception:
                return None


def _agreement(decision: str, oracle_same: bool) -> float:
    """Score a threshold band's decision against the oracle verdict. A deferral
    (``adjudicate``) is never wrong — it routes to the same oracle."""
    if decision == "merge":
        return 1.0 if oracle_same else 0.0
    if decision == "reject":
        return 0.0 if oracle_same else 1.0
    return 1.0  # adjudicate — defers to the oracle, always correct

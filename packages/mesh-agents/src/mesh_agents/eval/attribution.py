"""Fault attribution — turn accuracy/freshness failures into concerns (the gradient).

The accuracy eval says *which* beliefs are wrong or stale; it doesn't say *why* or
*what to change*. This module closes that gap: for each non-supported belief it
walks the provenance chain (belief → supporting claims → their sources + how fresh
they are) and attributes the fault to ONE component — the actuator the improvement
loop would change:

    extraction  a claim misreads its source        → the extract-source prompt
    synthesis   claims fine, belief overstates      → the synthesize-belief prompt
    freshness   the source is stale/outdated        → scout more / a dead connector
    coverage    nothing sourced covers the topic    → open an investigation
    confidence  the KB is confident on wrong beliefs→ calibration weights

Each attribution becomes an :class:`ImprovementConcern`. The eval only *accumulates*
these — it never edits the system. A derived tension counts the open concerns per
component and fires an A/B improve pass once they cross a threshold.

Attribution is one cheap LLM call per failing belief (a :class:`FaultDiagnoser`),
grounded on the belief + the judge's web verdict + the provenance we hand it — no
extra search. Provenance gathering is best-effort: a store hiccup degrades to an
un-provenanced diagnosis, never an exception.
"""
from __future__ import annotations

from typing import Any, Protocol

from mesh_models.improvement_concern import ConcernComponent, ImprovementConcern
from pydantic import BaseModel, Field

from mesh_agents.eval.models import AccuracyReport, BeliefGrade, FreshnessReport, Verdict

# How much accuracy a verdict is worth (mirrors the accuracy score's weighting);
# severity = (1 - weight) * judge_confidence, so a confidently-contradicted belief
# is the strongest gradient and a partially-supported one is weaker.
_VERDICT_WEIGHT: dict[Verdict, float] = {
    Verdict.supported: 1.0,
    Verdict.partially_supported: 0.5,
    Verdict.contradicted: 0.0,
    Verdict.unverifiable: 0.0,
}

# The routable actuator each component defaults to when the diagnoser doesn't name
# a more specific target.
_DEFAULT_TARGET: dict[ConcernComponent, str] = {
    ConcernComponent.scout: "scout-source",
    ConcernComponent.extraction: "extract-source",
    ConcernComponent.entity_resolution: "merge-candidate",
    ConcernComponent.synthesis: "synthesize-belief",
    ConcernComponent.challenge: "challenge-belief",
    ConcernComponent.confidence: "",
    ConcernComponent.decay: "",
    ConcernComponent.freshness: "",
    ConcernComponent.coverage: "",
    ConcernComponent.other: "",
}


class FaultDiagnosis(BaseModel):
    """Where one failing belief's fault lives + a one-line fix direction."""

    component: ConcernComponent
    target: str = ""  # a specific actuator handle (source_type/connector), optional
    summary: str = Field(default="", description="The fault + fix direction, one line.")


class FaultDiagnoser(Protocol):
    def diagnose(
        self, grade: BeliefGrade, provenance: dict[str, Any]
    ) -> FaultDiagnosis:
        """Attribute one failing belief's fault to a component, given the judge's
        verdict/rationale (on ``grade``) and its provenance."""
        ...


def severity_of(grade: BeliefGrade) -> float:
    """Attributed accuracy loss for one grade: worse verdict times judge certainty."""
    weight = _VERDICT_WEIGHT.get(grade.verdict, 0.0)
    return max(0.0, min(1.0, (1.0 - weight) * (grade.judge_confidence or 1.0)))


def _provenance(conn: Any, grade: BeliefGrade, freshness: FreshnessReport | None) -> dict[str, Any]:
    """Best-effort **execution trace** for a belief — enough for the diagnoser to
    attribute the fault to *any* pipeline stage, not just a prompt:

    - the belief's confidence + how many times it's been revised/challenged (a wrong
      belief that was never challenged → `challenge`; a stale one still held → `decay`);
    - each supporting claim's predicate, excerpt, and which agent extracted it
      (`extraction`);
    - the source each rests on: type, url, age (`scout` / `freshness`);
    - the field's stale source types + the live confidence weights.

    Never raises — a bad read yields the partial trace gathered so far."""
    prov: dict[str, Any] = {
        "belief": {}, "claims": [], "sources": [], "stale_source_types": [],
    }
    try:
        from mesh_db.beliefs import get_belief_by_id
        from mesh_db.claims import get_claim_by_id
        from mesh_db.revisions import list_revisions
        from mesh_db.sources import get_source_by_id

        belief = get_belief_by_id(conn, grade.belief_id) if grade.belief_id else None
        if belief is None:
            return prov
        revs = list_revisions(conn, belief_id=belief.id, limit=20)
        challengers = {
            r.revised_by_agent for r in revs
            if "skeptic" in (r.revised_by_agent or "").lower()
            or "challenge" in (r.revised_by_agent or "").lower()
        }
        prov["belief"] = {
            "stored_confidence": round(belief.confidence, 3),
            "n_supporting_claims": len(belief.supporting_claim_ids),
            "n_contradicting_claims": len(belief.contradicting_claim_ids),
            "n_revisions": len(revs),
            "ever_challenged": bool(challengers),
            "last_revised_at": str(getattr(belief, "last_revised_at", "") or ""),
        }
        seen_sources: set[str] = set()
        for cid in list(belief.supporting_claim_ids)[:8]:
            claim = get_claim_by_id(conn, cid)
            if claim is None:
                continue
            prov["claims"].append({
                "predicate": claim.predicate,
                "excerpt": (claim.raw_excerpt or "")[:240],
                "extracted_by": getattr(claim, "extracted_by_agent", "") or "",
            })
            if claim.source_id and claim.source_id not in seen_sources:
                seen_sources.add(claim.source_id)
                src = get_source_by_id(conn, claim.source_id)
                if src is not None:
                    prov["sources"].append({
                        "type": getattr(src.type, "value", str(src.type)),
                        "url": src.url,
                        "published_at": str(getattr(src, "published_at", "") or ""),
                    })
        if freshness is not None:
            prov["stale_source_types"] = list(freshness.stale_types)
        prov["confidence_weights"] = _confidence_weights()
    except Exception:
        return prov
    return prov


def _confidence_weights() -> dict[str, float]:
    """The live confidence weighting — so the diagnoser can flag a `confidence`
    calibration fault (KB confident on a belief the web contradicts)."""
    try:
        from mesh_agents.confidence import ConfidenceWeights

        w = ConfidenceWeights.from_env()
        return {"support_weight": w.support_weight, "attack_weight": w.attack_weight}
    except Exception:
        return {}


def attribute_report(
    conn: Any,
    report: AccuracyReport,
    diagnoser: FaultDiagnoser,
    *,
    freshness: FreshnessReport | None = None,
) -> list[ImprovementConcern]:
    """Attribute every non-supported belief in ``report`` to a component and return
    the resulting concerns (unwritten — the caller emits ``RecordConcernEffect``s).

    Fully-supported beliefs are skipped (no fault). A diagnoser failure on one
    belief is skipped, never fatal — one bad diagnosis can't sink the pass."""
    concerns: list[ImprovementConcern] = []
    for grade in report.grades:
        if grade.verdict in (Verdict.supported,):
            continue  # correct — no gradient here
        prov = _provenance(conn, grade, freshness)
        try:
            diag = diagnoser.diagnose(grade, prov)
        except Exception:
            continue
        component = diag.component
        target = (diag.target or "").strip() or _DEFAULT_TARGET.get(component, "")
        summary = diag.summary.strip() or grade.rationale[:240]
        concerns.append(
            ImprovementConcern(
                field_id=report.field_id,
                component=component,
                target=target,
                belief_id=grade.belief_id,
                verdict=grade.verdict.value,
                severity=severity_of(grade),
                summary=summary,
                evidence_urls=list(grade.evidence_urls),
            )
        )
    return concerns


# --------------------------------------------------------------------------
# the LLM diagnoser
# --------------------------------------------------------------------------

_DIAGNOSER_SYSTEM = """\
You do ROOT-CAUSE attribution for a knowledge base whose pipeline is:
scout (pull sources) → extract (claims from a source) → resolve (merge entities) →
synthesize (claims → a belief) → challenge (skeptic re-examines) → age (decay/archive
stale beliefs). You are given ONE belief the KB holds, an external judge's verdict on
it (with web evidence), and the belief's execution trace (its confidence + revision/
challenge history, the supporting claims + who extracted them, the sources + their
age, stale source types, and the live confidence weights). Decide which ONE pipeline
stage is MOST responsible for the belief being wrong / unverifiable, and give a
one-line fix direction. An inaccurate belief can be any stage's fault — do not
default to the prompt.

Stages:
- scout: the wrong or low-quality sources were pulled in the first place (an
  unreliable source type, an off-topic feed). Fix: connector config / query.
- extraction: a supporting claim misreads or overreaches its source excerpt (the
  excerpt doesn't say what the claim says). Fix: the extraction prompt.
- entity_resolution: the belief conflates two different entities or split one (a
  bad merge). Fix: resolution thresholds.
- synthesis: the claims are individually fine but the belief overstates,
  mis-aggregates, or over-generalizes them. Fix: the synthesis prompt.
- challenge: the belief is plainly wrong yet was never challenged (ever_challenged
  is false and confidence stayed high). Fix: the skeptic/challenge prompt or cadence.
- confidence: the belief is wrong yet the KB holds it confidently — a calibration
  problem (the confidence weights), not a content one.
- decay: the belief WAS right but is old and should have been aged out, and wasn't.
  Fix: decay/archival thresholds.
- freshness: the belief's sources are stale and the world moved on (a stale source
  type / a connector that stopped delivering). Fix: scout more often.
- coverage: nothing in the sources actually covers this claim — unverifiable or
  unfounded. Fix: gather new evidence.

Use the trace: excerpts don't back the belief -> extraction; they back narrower
claims than stated -> synthesis; sources old / stale type flagged -> freshness;
no real supporting sources -> coverage; wrong + high confidence + never challenged
-> challenge or confidence. Return only valid JSON."""


class LLMFaultDiagnoser:
    """Attribute faults with the configured mesh LLM (structured output, no search —
    grounded on the provided verdict + provenance, so the cheap tier is fine)."""

    def __init__(self, llm: Any | None = None) -> None:
        if llm is None:
            from mesh_llm import make_llm_client

            llm = make_llm_client()
        self._llm = llm

    def diagnose(self, grade: BeliefGrade, provenance: dict[str, Any]) -> FaultDiagnosis:
        import json

        user = "\n".join(
            [
                f"BELIEF: {grade.statement}",
                f"STORED CONFIDENCE: {grade.stored_confidence:.2f}",
                f"JUDGE VERDICT: {grade.verdict.value} (certainty {grade.judge_confidence:.2f})",
                f"JUDGE RATIONALE: {grade.rationale}",
                f"WEB EVIDENCE: {', '.join(grade.evidence_urls) or '(none)'}",
                f"PROVENANCE: {json.dumps(provenance, ensure_ascii=False)[:2000]}",
            ]
        )
        diag, _latency, _usage = self._llm.complete_with_usage(
            "eval_fault_diagnoser", _DIAGNOSER_SYSTEM, user, FaultDiagnosis
        )
        return diag if isinstance(diag, FaultDiagnosis) else FaultDiagnosis(
            component=ConcernComponent.other
        )

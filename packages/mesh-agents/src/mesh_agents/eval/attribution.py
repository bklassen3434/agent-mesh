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
    ConcernComponent.extraction: "extract-source",
    ConcernComponent.synthesis: "synthesize-belief",
    ConcernComponent.confidence: "",
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
    """Best-effort provenance for a belief: its supporting claims' excerpts +
    predicates and the source (type, url, published age) each rests on, plus the
    field's per-source-type freshness. Never raises — a bad read yields {}."""
    prov: dict[str, Any] = {"claims": [], "sources": [], "stale_source_types": []}
    try:
        from mesh_db.beliefs import get_belief_by_id
        from mesh_db.claims import get_claim_by_id
        from mesh_db.sources import get_source_by_id

        belief = get_belief_by_id(conn, grade.belief_id) if grade.belief_id else None
        if belief is None:
            return prov
        seen_sources: set[str] = set()
        for cid in list(belief.supporting_claim_ids)[:8]:
            claim = get_claim_by_id(conn, cid)
            if claim is None:
                continue
            prov["claims"].append(
                {"predicate": claim.predicate, "excerpt": (claim.raw_excerpt or "")[:240]}
            )
            if claim.source_id and claim.source_id not in seen_sources:
                seen_sources.add(claim.source_id)
                src = get_source_by_id(conn, claim.source_id)
                if src is not None:
                    prov["sources"].append(
                        {
                            "type": getattr(src.type, "value", str(src.type)),
                            "url": src.url,
                            "published_at": str(getattr(src, "published_at", "") or ""),
                        }
                    )
        if freshness is not None:
            prov["stale_source_types"] = list(freshness.stale_types)
    except Exception:
        return prov
    return prov


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
You do ROOT-CAUSE attribution for a knowledge base that extracts factual claims
from sources and synthesizes them into beliefs. You are given ONE belief the
knowledge base holds, an external judge's verdict on it (with web evidence), and
the belief's provenance (the claims + sources it rests on, and which source types
are stale). Decide which ONE component is most responsible for the belief being
wrong / unverifiable, and give a one-line fix direction.

Components:
- extraction: a supporting claim misreads or overreaches its source excerpt (the
  excerpt doesn't actually say what the claim says). Fix: the extraction prompt.
- synthesis: the claims are individually fine but the belief overstates,
  mis-aggregates, or mis-generalizes them. Fix: the synthesis prompt.
- freshness: the belief WAS right but its sources are stale and the world moved
  on (a stale source type / a connector that stopped delivering). Fix: scout more.
- coverage: nothing in the sources actually covers this claim, so it's unverifiable
  or unfounded. Fix: gather new evidence.
- confidence: the belief is wrong yet the KB holds it confidently — a calibration
  problem more than a content one.

Prefer the component the provenance actually supports. If the excerpts don't back
the belief -> extraction; if they back narrower claims than the belief states ->
synthesis; if sources are old / a stale type is flagged -> freshness; if there are
no real supporting sources -> coverage. Return only valid JSON."""


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

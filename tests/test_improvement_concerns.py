"""Tests for the concern accumulator (attribution → concerns → store → effects).

- **No DB** — attribution logic with a stub diagnoser and no connection: supported
  beliefs make no concern, non-supported ones do, severity tracks the verdict.
- **DB (testcontainers)** — the append-only store: record + open-dedup, grouped
  counts (the activation signal), resolve; and the RecordConcern/ResolveConcerns
  effects through the write gateway.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mesh_agents.eval.attribution import FaultDiagnosis, attribute_report, severity_of
from mesh_agents.eval.models import AccuracyReport, BeliefGrade, Verdict
from mesh_models.improvement_concern import (
    ConcernComponent,
    ImprovementConcern,
)


def _grade(verdict: Verdict, *, bid: str = "b1", jc: float = 1.0) -> BeliefGrade:
    return BeliefGrade(
        belief_id=bid,
        statement=f"belief {bid}",
        stored_confidence=0.8,
        verdict=verdict,
        judge_confidence=jc,
        rationale="the web says otherwise",
        evidence_urls=["https://example.com/x"],
    )


def _report(*grades: BeliefGrade) -> AccuracyReport:
    return AccuracyReport(
        field_id="fld",
        generated_at=datetime.now(UTC),
        sample_size=len(grades),
        strategy="recent",
        judge_model="stub",
        grades=list(grades),
    )


class _StubDiagnoser:
    def __init__(self, component: ConcernComponent, target: str = "") -> None:
        self._c, self._t = component, target
        self.calls = 0

    def diagnose(self, grade: BeliefGrade, provenance: dict[str, Any]) -> FaultDiagnosis:
        self.calls += 1
        return FaultDiagnosis(component=self._c, target=self._t, summary="fix X")


# --------------------------------------------------------------------------
# attribution (no DB — provenance degrades to empty on conn=None)
# --------------------------------------------------------------------------


def test_supported_beliefs_produce_no_concern() -> None:
    diag = _StubDiagnoser(ConcernComponent.extraction)
    concerns = attribute_report(
        None, _report(_grade(Verdict.supported), _grade(Verdict.supported, bid="b2")),
        diag,
    )
    assert concerns == []
    assert diag.calls == 0  # never even diagnosed a correct belief


def test_non_supported_beliefs_become_concerns_with_default_target() -> None:
    diag = _StubDiagnoser(ConcernComponent.extraction)  # no explicit target
    concerns = attribute_report(
        None,
        _report(
            _grade(Verdict.contradicted, bid="b1"),
            _grade(Verdict.partially_supported, bid="b2"),
            _grade(Verdict.supported, bid="b3"),
        ),
        diag,
    )
    assert [c.belief_id for c in concerns] == ["b1", "b2"]
    # extraction component defaults to the extract-source actuator
    assert all(c.target == "extract-source" for c in concerns)
    assert all(c.component is ConcernComponent.extraction for c in concerns)
    assert concerns[0].evidence_urls == ["https://example.com/x"]


def test_full_pipeline_components_route_to_their_actuators() -> None:
    # The fault can be any stage, not just a prompt — each maps to its actuator.
    cases = {
        ConcernComponent.scout: "scout-source",
        ConcernComponent.entity_resolution: "merge-candidate",
        ConcernComponent.challenge: "challenge-belief",
        ConcernComponent.synthesis: "synthesize-belief",
    }
    for component, target in cases.items():
        concerns = attribute_report(
            None, _report(_grade(Verdict.contradicted)), _StubDiagnoser(component)
        )
        assert concerns[0].component is component
        assert concerns[0].target == target


def test_severity_tracks_verdict_and_judge_certainty() -> None:
    # contradicted @ certainty 1.0 -> full loss; partially_supported -> half.
    assert severity_of(_grade(Verdict.contradicted, jc=1.0)) == 1.0
    assert severity_of(_grade(Verdict.contradicted, jc=0.5)) == 0.5
    assert severity_of(_grade(Verdict.partially_supported, jc=1.0)) == 0.5
    assert severity_of(_grade(Verdict.supported, jc=1.0)) == 0.0


def test_a_diagnoser_failure_skips_that_belief_not_the_pass() -> None:
    class _Flaky:
        def diagnose(self, grade: BeliefGrade, provenance: dict[str, Any]) -> FaultDiagnosis:
            if grade.belief_id == "b1":
                raise RuntimeError("llm hiccup")
            return FaultDiagnosis(component=ConcernComponent.synthesis, summary="s")

    concerns = attribute_report(
        None,
        _report(_grade(Verdict.contradicted, bid="b1"), _grade(Verdict.contradicted, bid="b2")),
        _Flaky(),
    )
    assert [c.belief_id for c in concerns] == ["b2"]
    assert concerns[0].target == "synthesize-belief"


# --------------------------------------------------------------------------
# the store (DB) — accumulate, dedupe-while-open, group-count, resolve
# --------------------------------------------------------------------------


_TARGET = {
    ConcernComponent.extraction: "extract-source",
    ConcernComponent.synthesis: "synthesize-belief",
}


def _concern(component: ConcernComponent, *, bid: str,
             severity: float = 1.0) -> ImprovementConcern:
    from mesh_models.field import DEFAULT_FIELD_ID

    return ImprovementConcern(
        field_id=DEFAULT_FIELD_ID, component=component, target=_TARGET.get(component, ""),
        belief_id=bid, verdict="contradicted", severity=severity, summary="s",
    )


def _reset(conn: Any) -> str:
    from mesh_models.field import DEFAULT_FIELD_ID

    conn.execute("DELETE FROM improvement_concerns WHERE field_id = %s", [DEFAULT_FIELD_ID])
    return DEFAULT_FIELD_ID


def test_record_dedupes_an_already_open_concern(tmp_db: Any) -> None:
    from mesh_db.improvement_concerns import open_concern_groups, record_concern

    field_id = _reset(tmp_db)
    assert record_concern(tmp_db, _concern(ConcernComponent.extraction, bid="b1")) is not None
    # same (field, component, target, belief) still open -> deduped
    assert record_concern(tmp_db, _concern(ConcernComponent.extraction, bid="b1")) is None
    # a different belief is new evidence
    assert record_concern(tmp_db, _concern(ConcernComponent.extraction, bid="b2")) is not None

    groups = open_concern_groups(tmp_db, field_id)
    ext = next(g for g in groups if g.component == "extraction")
    assert ext.count == 2 and ext.severity == 2.0
    _reset(tmp_db)


def test_resolve_closes_concerns_and_drops_them_from_the_open_count(tmp_db: Any) -> None:
    from mesh_db.improvement_concerns import (
        list_open_concerns,
        open_concern_groups,
        record_concern,
        resolve_concerns,
    )

    field_id = _reset(tmp_db)
    syn = ConcernComponent.synthesis
    c1 = record_concern(tmp_db, _concern(syn, bid="b1"))
    record_concern(tmp_db, _concern(syn, bid="b2"))
    assert len(list_open_concerns(tmp_db, field_id, syn)) == 2

    closed = resolve_concerns(tmp_db, [c1.id], resolved_by="ver-123")  # type: ignore[union-attr]
    assert closed == 1
    remaining = list_open_concerns(tmp_db, field_id, syn)
    assert [c.belief_id for c in remaining] == ["b2"]
    # a resolved belief can be re-filed later (dedup only blocks while OPEN)
    assert record_concern(tmp_db, _concern(syn, bid="b1")) is not None
    assert not [g for g in open_concern_groups(tmp_db, field_id) if g.component == "extraction"]
    _reset(tmp_db)


def test_record_and_resolve_concern_effects_through_the_gateway(tmp_db: Any) -> None:
    from mesh_db.effects import apply_effects
    from mesh_db.improvement_concerns import list_open_concerns
    from mesh_models.effect import RecordConcernEffect, ResolveConcernsEffect

    field_id = _reset(tmp_db)
    c = _concern(ConcernComponent.coverage, bid="b9")
    rep = apply_effects(tmp_db, [RecordConcernEffect(concern=c)])
    assert rep.concerns_recorded == 1
    # re-recording the same open concern is deduped by the gateway (not counted)
    rep2 = apply_effects(tmp_db, [RecordConcernEffect(concern=c)])
    assert rep2.concerns_recorded == 0
    assert len(list_open_concerns(tmp_db, field_id, ConcernComponent.coverage)) == 1

    rep3 = apply_effects(tmp_db, [ResolveConcernsEffect(concern_ids=[c.id], resolved_by="v1")])
    assert rep3.concerns_resolved == 1
    assert list_open_concerns(tmp_db, field_id, ConcernComponent.coverage) == []
    _reset(tmp_db)

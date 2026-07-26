"""Result models for the accuracy + freshness eval.

All read-only, all serializable — a run writes one JSON report the CLI can
print and archive (so ``eval-accuracy`` output is trend-able over time).
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Verdict(StrEnum):
    """A judge's grade of one belief against external evidence."""

    supported = "supported"
    partially_supported = "partially_supported"
    contradicted = "contradicted"
    unverifiable = "unverifiable"


# Weight each verdict contributes to the accuracy score (unverifiable is
# excluded from the denominator entirely — see AccuracyReport.accuracy_score).
_VERDICT_WEIGHT: dict[Verdict, float] = {
    Verdict.supported: 1.0,
    Verdict.partially_supported: 0.5,
    Verdict.contradicted: 0.0,
}


class BeliefGrade(BaseModel):
    """One belief, graded against the live web."""

    belief_id: str
    statement: str
    stored_confidence: float  # the KB's own confidence in this belief
    verdict: Verdict
    judge_confidence: float = Field(ge=0.0, le=1.0)  # the judge's certainty
    rationale: str
    evidence_urls: list[str] = Field(default_factory=list)


class FreshnessRow(BaseModel):
    """Freshness of one source type within a field."""

    source_type: str
    count: int
    newest_published: datetime | None
    newest_fetched: datetime | None
    age_hours: float | None  # now - newest_published; None if never published
    stale: bool


class FreshnessReport(BaseModel):
    field_id: str
    generated_at: datetime
    stale_after_hours: float
    newest_source_age_hours: float | None  # headline: age of the freshest source
    rows: list[FreshnessRow]

    @property
    def ok(self) -> bool:
        """True when the field as a whole has fresh content (its newest source
        is within the staleness window). A single stale type is reported but
        does not fail the field if something else is current."""
        age = self.newest_source_age_hours
        return age is not None and age <= self.stale_after_hours

    @property
    def stale_types(self) -> list[str]:
        return [r.source_type for r in self.rows if r.stale]


class AccuracyReport(BaseModel):
    field_id: str
    generated_at: datetime
    sample_size: int
    strategy: str
    judge_model: str
    grades: list[BeliefGrade]
    judge_tokens: int = 0

    def count(self, verdict: Verdict) -> int:
        return sum(1 for g in self.grades if g.verdict == verdict)

    @property
    def verifiable(self) -> int:
        """Graded beliefs the judge could actually check (verdict != unverifiable)."""
        return sum(1 for g in self.grades if g.verdict != Verdict.unverifiable)

    @property
    def accuracy_score(self) -> float | None:
        """Weighted correctness over the *verifiable* sample.

        ``(supported + 0.5*partial) / verifiable``. None when nothing in the
        sample could be verified (report the unverifiable rate instead of a
        misleading 0/0)."""
        n = self.verifiable
        if n == 0:
            return None
        earned = sum(_VERDICT_WEIGHT.get(g.verdict, 0.0) for g in self.grades)
        return earned / n

    @property
    def unverifiable_rate(self) -> float | None:
        if not self.grades:
            return None
        return self.count(Verdict.unverifiable) / len(self.grades)

    @property
    def contradicted(self) -> list[BeliefGrade]:
        """The beliefs the judge found the web contradicts — the ones to fix."""
        return [g for g in self.grades if g.verdict == Verdict.contradicted]

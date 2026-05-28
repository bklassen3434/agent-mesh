"""Curator agent — ranks existing beliefs by how worth-challenging they are.

Pure / rule-based. No LLM. The coordinator (skeptic_sweep) pre-fetches all
held beliefs, hydrates per-belief metadata (last_challenged_at, recent
contradicting activity) from the revisions table, and asks Curator which N
beliefs deserve a Skeptic round.

Selection heuristics are deterministic and documented in score_belief() so
operators can tune the weights without reading prompt code.
"""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any

from mesh_a2a.card_builder import build_agent_card
from mesh_a2a.task_server import build_task_app
from pydantic import BaseModel, Field
from starlette.applications import Starlette

from mesh_agents.base import BaseAgent

_W_AGE = 1.0
_W_WEAKNESS = 1.0
_W_EXTREMITY = 1.0
_W_CONTRADICTION_BOOST = 0.5
_W_COOLDOWN_PENALTY = 1.0
_DEFAULT_COOLDOWN_DAYS = 7
# Phase 6a: evidence-staleness signal (max-extracted-at across the belief's
# supporting + contradicting claims) is additive on top of the existing
# age signal, which only tracks last_revised_at on the belief itself.
_DEFAULT_STALENESS_WEIGHT = 0.3


def _staleness_weight() -> float:
    raw = os.environ.get("MESH_CURATOR_STALENESS_WEIGHT")
    if raw is None:
        return _DEFAULT_STALENESS_WEIGHT
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_STALENESS_WEIGHT


class BeliefForCuration(BaseModel):
    belief_id: str
    topic: str
    statement: str
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_claim_count: int = 0
    contradicting_claim_count: int = 0
    last_revised_at: datetime
    last_challenged_at: datetime | None = None
    recent_contradicting_activity: bool = False
    last_evidence_at: datetime | None = None


class CuratorInput(BaseModel):
    beliefs: list[BeliefForCuration] = Field(default_factory=list)
    pick_count: int = 5
    now: datetime = Field(default_factory=lambda: datetime.now(UTC))
    cooldown_days: int = _DEFAULT_COOLDOWN_DAYS


class CuratorPick(BaseModel):
    belief_id: str
    score: float
    rationale: str


class InvestigationSuggestion(BaseModel):
    """Phase 7a: Curator-emitted suggestion that an Investigation be opened
    on a belief. Orchestrator-side (skeptic-sweep) translates these into
    Investigation rows.

    The Curator suggests *what* to investigate; the orchestrator decides
    *target_entity_id* (from the belief's supporting claims) and persists.
    """

    belief_id: str
    hypothesis: str
    suggested_source_types: list[str] = Field(default_factory=list)
    rationale: str


class CuratorOutput(BaseModel):
    picks: list[CuratorPick] = Field(default_factory=list)
    investigation_suggestions: list[InvestigationSuggestion] = Field(default_factory=list)


# Phase 2 A2A skill types ----------------------------------------------------


class SelectBeliefsSkillInput(BaseModel):
    beliefs: list[dict[str, Any]] = Field(default_factory=list)
    pick_count: int = 5
    now: datetime | None = None
    cooldown_days: int = _DEFAULT_COOLDOWN_DAYS


class SelectBeliefsSkillOutput(BaseModel):
    picks: list[CuratorPick]
    investigation_suggestions: list[InvestigationSuggestion] = Field(default_factory=list)


# Pure scoring logic ---------------------------------------------------------


def score_belief(
    b: BeliefForCuration, now: datetime, cooldown_days: int
) -> tuple[float, str]:
    """Compute (score, rationale) for a single belief.

    Higher score = more deserving of a skeptic round.

    Factors (each in [0,1] before weighting except cooldown penalty):
      - age: staler beliefs (longer since last revision) score higher
      - supporting-weakness: fewer supporting claims = higher score
      - confidence-extremity: very high or very low confidence is interesting
      - contradicting-activity boost: recent contradictions add a flat bump
      - cooldown penalty: subtract if Skeptic looked at this belief recently
    """
    days_since_revised = max(0.0, (now - b.last_revised_at).total_seconds() / 86400.0)
    age = min(1.0, days_since_revised / 90.0)
    weakness = 1.0 / (1 + b.supporting_claim_count)
    extremity = 2.0 * abs(b.confidence - 0.5)
    contradiction_boost = _W_CONTRADICTION_BOOST if b.recent_contradicting_activity else 0.0

    # Evidence staleness: time since the most recent supporting/contradicting
    # claim arrived. No claims at all → max staleness (1.0).
    if b.last_evidence_at is None:
        evidence_staleness = 1.0
        evidence_days: float | None = None
    else:
        evidence_days = max(
            0.0, (now - b.last_evidence_at).total_seconds() / 86400.0
        )
        evidence_staleness = min(1.0, evidence_days / 90.0)
    staleness_weight = _staleness_weight()

    cooldown_penalty = 0.0
    in_cooldown = False
    if b.last_challenged_at is not None:
        days_since_challenged = (now - b.last_challenged_at).total_seconds() / 86400.0
        if days_since_challenged < cooldown_days:
            cooldown_penalty = _W_COOLDOWN_PENALTY
            in_cooldown = True

    score = (
        _W_AGE * age
        + _W_WEAKNESS * weakness
        + _W_EXTREMITY * extremity
        + contradiction_boost
        + staleness_weight * evidence_staleness
        - cooldown_penalty
    )

    parts: list[str] = [
        f"age={age:.2f} (revised {days_since_revised:.0f}d ago)",
        f"weakness={weakness:.2f} ({b.supporting_claim_count} supporters)",
        f"extremity={extremity:.2f} (conf={b.confidence:.2f})",
    ]
    if evidence_days is None:
        parts.append(f"staleness={evidence_staleness:.2f} (no claims)")
    else:
        parts.append(
            f"staleness={evidence_staleness:.2f} "
            f"(last evidence {evidence_days:.0f}d ago)"
        )
    if contradiction_boost:
        parts.append("recent contradictions +0.5")
    if in_cooldown:
        parts.append(f"cooldown -{_W_COOLDOWN_PENALTY} (challenged within {cooldown_days}d)")
    rationale = "; ".join(parts)
    return score, rationale


def _suggested_source_types_for(topic: str) -> list[str]:
    """Pick scout types likely to surface fresh evidence on the topic.

    Heuristic-only — no LLM call. The defaults cover the "primary
    research" pipeline (arxiv) plus the corroboration sources
    (leaderboards, curated blogs). Adds github when the topic touches
    code / models / repos. Adds social when the topic is broad enough
    that practitioner discussion likely beats formal papers.
    """
    base = ["arxiv", "leaderboard", "blog"]
    topic_l = topic.lower()
    if any(t in topic_l for t in ("repo", "code", "library", "framework", "open-source")):
        base.append("github")
    if any(t in topic_l for t in ("adoption", "practice", "tooling", "ops", "deploy")):
        base.extend(["hn", "reddit"])
    # Preserve order, dedup
    seen: set[str] = set()
    out: list[str] = []
    for s in base:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _should_investigate(b: BeliefForCuration, now: datetime) -> tuple[bool, str]:
    """Decide whether a belief warrants opening an Investigation.

    Heuristic: any of these signals trip the threshold —
    - belief has fewer than 2 supporters AND has been around > 14 days
      (thin evidence base that hasn't grown)
    - evidence is stale (> 60 days since the most recent supporting/
      contradicting claim arrived)
    - recent contradicting activity (someone challenged it lately and
      it deserves a fresh round of evidence-gathering)
    """
    reasons: list[str] = []
    days_since_revised = max(0.0, (now - b.last_revised_at).total_seconds() / 86400.0)
    if b.supporting_claim_count < 2 and days_since_revised > 14:
        reasons.append(
            f"thin evidence ({b.supporting_claim_count} supporters, "
            f"{days_since_revised:.0f}d old)"
        )
    if b.last_evidence_at is None:
        reasons.append("no claims attached yet")
    else:
        evidence_age = (now - b.last_evidence_at).total_seconds() / 86400.0
        if evidence_age > 60:
            reasons.append(f"evidence is {evidence_age:.0f}d old")
    if b.recent_contradicting_activity:
        reasons.append("recent contradicting activity")
    if not reasons:
        return False, ""
    return True, "; ".join(reasons)


def select_beliefs_to_challenge_pure(input: CuratorInput) -> CuratorOutput:
    if not input.beliefs:
        return CuratorOutput(picks=[])
    scored = [
        (score_belief(b, input.now, input.cooldown_days), b) for b in input.beliefs
    ]
    scored.sort(key=lambda x: x[0][0], reverse=True)
    picks = [
        CuratorPick(belief_id=b.belief_id, score=score, rationale=rationale)
        for (score, rationale), b in scored[: input.pick_count]
    ]

    # Investigation suggestions are independent of the Skeptic pick set —
    # any belief in the input that trips the threshold qualifies. A belief
    # can both go to Skeptic for falsification AND get an investigation
    # opened to gather more evidence; these are parallel paths.
    suggestions: list[InvestigationSuggestion] = []
    for _, belief in scored:
        should, reason = _should_investigate(belief, input.now)
        if not should:
            continue
        suggestions.append(
            InvestigationSuggestion(
                belief_id=belief.belief_id,
                hypothesis=(
                    f"Is the belief '{belief.statement}' "
                    f"(topic: {belief.topic}) still supported by recent evidence?"
                ),
                suggested_source_types=_suggested_source_types_for(belief.topic),
                rationale=reason,
            )
        )
    return CuratorOutput(picks=picks, investigation_suggestions=suggestions)


async def _handle_select_beliefs(payload: dict[str, Any]) -> dict[str, Any]:
    skill_input = SelectBeliefsSkillInput.model_validate(payload)
    agent_input = CuratorInput(
        beliefs=[BeliefForCuration.model_validate(b) for b in skill_input.beliefs],
        pick_count=skill_input.pick_count,
        now=skill_input.now or datetime.now(UTC),
        cooldown_days=skill_input.cooldown_days,
    )
    output = select_beliefs_to_challenge_pure(agent_input)
    return SelectBeliefsSkillOutput(
        picks=output.picks,
        investigation_suggestions=output.investigation_suggestions,
    ).model_dump(mode="json")


class CuratorAgent(BaseAgent):
    name = "curator"

    def __init__(self, llm: Any | None = None, db_conn: Any | None = None) -> None:
        super().__init__(llm=llm, db_conn=db_conn)

    async def run(self, input: BaseModel) -> CuratorOutput:
        assert isinstance(input, CuratorInput)
        return await asyncio.to_thread(select_beliefs_to_challenge_pure, input)

    def to_a2a_server(self, url: str) -> Starlette:
        card = build_agent_card(
            name="Curator",
            description=(
                "Ranks held beliefs by how worth-challenging they are; returns the "
                "top-N belief IDs for the Skeptic to assess."
            ),
            url=url,
            skill_id="select_beliefs_to_challenge",
            skill_name="Select Beliefs To Challenge",
            skill_description=(
                "Score beliefs by staleness, supporter count, confidence extremity, "
                "recent contradicting activity, and a cooldown on recently-challenged ones."
            ),
            skill_tags=["curator", "falsification", "selection"],
        )
        return build_task_app(
            agent_card=card,
            skill_handlers={"select_beliefs_to_challenge": _handle_select_beliefs},
            agent_name="curator",
        )

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from mesh_agents.curator import (
    BeliefForCuration,
    CuratorAgent,
    CuratorInput,
    select_beliefs_to_challenge_pure,
)

_NOW = datetime(2026, 5, 25, tzinfo=UTC)


def _belief(
    belief_id: str,
    *,
    confidence: float = 0.5,
    supporting: int = 3,
    contradicting: int = 0,
    revised_days_ago: int = 30,
    challenged_days_ago: int | None = None,
    recent_contradiction: bool = False,
    evidence_days_ago: int | None = 0,
) -> BeliefForCuration:
    """Build a curation candidate.

    ``evidence_days_ago=0`` means fresh evidence (today). Pass ``None`` to
    simulate "no claims attached" — that path is the maximum-staleness
    fallback in score_belief.
    """
    return BeliefForCuration(
        belief_id=belief_id,
        topic="sota:test",
        statement=f"belief {belief_id}",
        confidence=confidence,
        supporting_claim_count=supporting,
        contradicting_claim_count=contradicting,
        last_revised_at=_NOW - timedelta(days=revised_days_ago),
        last_challenged_at=(
            _NOW - timedelta(days=challenged_days_ago)
            if challenged_days_ago is not None
            else None
        ),
        recent_contradicting_activity=recent_contradiction,
        last_evidence_at=(
            _NOW - timedelta(days=evidence_days_ago)
            if evidence_days_ago is not None
            else None
        ),
    )


class TestCuratorAgent:
    def test_empty_input_returns_empty_picks(self) -> None:
        out = select_beliefs_to_challenge_pure(CuratorInput(beliefs=[], now=_NOW))
        assert out.picks == []

    def test_stale_belief_scores_higher_than_fresh(self) -> None:
        beliefs = [
            _belief("fresh", revised_days_ago=2),
            _belief("stale", revised_days_ago=120),
        ]
        out = select_beliefs_to_challenge_pure(
            CuratorInput(beliefs=beliefs, now=_NOW, pick_count=2)
        )
        assert out.picks[0].belief_id == "stale"
        assert out.picks[1].belief_id == "fresh"

    def test_extreme_confidence_scores_higher_than_middling(self) -> None:
        beliefs = [
            _belief("middling", confidence=0.5, revised_days_ago=30, supporting=3),
            _belief("extreme", confidence=0.95, revised_days_ago=30, supporting=3),
        ]
        out = select_beliefs_to_challenge_pure(
            CuratorInput(beliefs=beliefs, now=_NOW, pick_count=2)
        )
        assert out.picks[0].belief_id == "extreme"

    def test_fewer_supporters_scores_higher(self) -> None:
        beliefs = [
            _belief("well-supported", supporting=20, revised_days_ago=30),
            _belief("thin", supporting=1, revised_days_ago=30),
        ]
        out = select_beliefs_to_challenge_pure(
            CuratorInput(beliefs=beliefs, now=_NOW, pick_count=2)
        )
        assert out.picks[0].belief_id == "thin"

    def test_recent_contradicting_activity_boosts_score(self) -> None:
        beliefs = [
            _belief("quiet", recent_contradiction=False),
            _belief("noisy", recent_contradiction=True),
        ]
        out = select_beliefs_to_challenge_pure(
            CuratorInput(beliefs=beliefs, now=_NOW, pick_count=2)
        )
        assert out.picks[0].belief_id == "noisy"

    def test_cooldown_penalty_demotes_recently_challenged(self) -> None:
        # Both beliefs are stale enough to be top picks. The one within the
        # cooldown window should still get penalized below a comparable belief
        # that hasn't been challenged yet.
        beliefs = [
            _belief("recently-challenged", challenged_days_ago=2, revised_days_ago=60),
            _belief("never-challenged", challenged_days_ago=None, revised_days_ago=60),
        ]
        out = select_beliefs_to_challenge_pure(
            CuratorInput(beliefs=beliefs, now=_NOW, pick_count=2, cooldown_days=7)
        )
        assert out.picks[0].belief_id == "never-challenged"
        # Confirm the rationale explains why the other was demoted
        recently = next(p for p in out.picks if p.belief_id == "recently-challenged")
        assert "cooldown" in recently.rationale

    def test_cooldown_expires_after_window(self) -> None:
        beliefs = [
            _belief("old-challenge", challenged_days_ago=30, revised_days_ago=60),
            _belief("never-challenged", challenged_days_ago=None, revised_days_ago=60),
        ]
        out = select_beliefs_to_challenge_pure(
            CuratorInput(beliefs=beliefs, now=_NOW, pick_count=2, cooldown_days=7)
        )
        # Outside the cooldown window — the two should score essentially the
        # same and neither rationale should mention cooldown.
        for pick in out.picks:
            assert "cooldown" not in pick.rationale

    def test_returns_at_most_pick_count(self) -> None:
        beliefs = [_belief(f"b{i}", revised_days_ago=10 + i) for i in range(10)]
        out = select_beliefs_to_challenge_pure(
            CuratorInput(beliefs=beliefs, now=_NOW, pick_count=3)
        )
        assert len(out.picks) == 3

    def test_sorted_by_score_descending(self) -> None:
        beliefs = [
            _belief("a", revised_days_ago=2, supporting=20),
            _belief("b", revised_days_ago=200, supporting=1, confidence=0.95),
            _belief("c", revised_days_ago=50, supporting=5, confidence=0.6),
        ]
        out = select_beliefs_to_challenge_pure(
            CuratorInput(beliefs=beliefs, now=_NOW, pick_count=3)
        )
        scores = [p.score for p in out.picks]
        assert scores == sorted(scores, reverse=True)

    def test_stale_evidence_boosts_score_over_fresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Hold all other factors equal — only evidence age differs. The
        # default 0.3 weight should still flip the order.
        monkeypatch.delenv("MESH_CURATOR_STALENESS_WEIGHT", raising=False)
        beliefs = [
            _belief("fresh-evidence", evidence_days_ago=1),
            _belief("stale-evidence", evidence_days_ago=120),
        ]
        out = select_beliefs_to_challenge_pure(
            CuratorInput(beliefs=beliefs, now=_NOW, pick_count=2)
        )
        assert out.picks[0].belief_id == "stale-evidence"

    def test_no_evidence_gets_max_staleness(self) -> None:
        beliefs = [
            _belief("has-evidence", evidence_days_ago=1),
            _belief("no-evidence", evidence_days_ago=None),
        ]
        out = select_beliefs_to_challenge_pure(
            CuratorInput(beliefs=beliefs, now=_NOW, pick_count=2)
        )
        assert out.picks[0].belief_id == "no-evidence"
        no_ev = next(p for p in out.picks if p.belief_id == "no-evidence")
        assert "no claims" in no_ev.rationale

    def test_staleness_weight_env_override_changes_ranking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With staleness weight at zero the stale-evidence advantage
        # disappears — the two beliefs are otherwise identical so order
        # is no longer determined by evidence age.
        monkeypatch.setenv("MESH_CURATOR_STALENESS_WEIGHT", "0.0")
        beliefs = [
            _belief("fresh-evidence", evidence_days_ago=1),
            _belief("stale-evidence", evidence_days_ago=120),
        ]
        out = select_beliefs_to_challenge_pure(
            CuratorInput(beliefs=beliefs, now=_NOW, pick_count=2)
        )
        # Both score identically, but sort is stable — confirm scores match
        assert out.picks[0].score == out.picks[1].score

    def test_async_run_round_trips(self) -> None:
        beliefs = [_belief("only", revised_days_ago=30)]
        agent = CuratorAgent()
        out = asyncio.run(agent.run(CuratorInput(beliefs=beliefs, now=_NOW, pick_count=5)))
        assert len(out.picks) == 1
        assert out.picks[0].belief_id == "only"

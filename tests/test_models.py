from __future__ import annotations

import pytest
from mesh_models.belief import Belief
from mesh_models.claim import (
    PREDICATE_TO_CLAIM_TYPE,
    Claim,
    ClaimStatus,
    ClaimType,
    claim_type_for_predicate,
)
from mesh_models.entity import Entity, EntityType
from mesh_models.investigation import Investigation, InvestigationStatus
from mesh_models.relationship import Relationship
from mesh_models.revision import BeliefRevision
from mesh_models.source import Source, SourceType
from pydantic import ValidationError


class TestEntityModel:
    def test_defaults(self) -> None:
        e = Entity(canonical_name="GPT-4", type=EntityType.model)
        assert e.aliases == []
        assert e.attributes == {}
        assert e.id != ""

    def test_enum_values(self) -> None:
        for t in EntityType:
            e = Entity(canonical_name="x", type=t)
            assert e.type == t

    def test_invalid_type(self) -> None:
        with pytest.raises(ValidationError):
            Entity(canonical_name="x", type="not_a_type")  # type: ignore[arg-type]

    def test_aliases_stored(self) -> None:
        e = Entity(canonical_name="BERT", type=EntityType.model, aliases=["bert-base", "bert"])
        assert "bert-base" in e.aliases


class TestSourceModel:
    def test_reliability_default(self) -> None:
        from datetime import UTC, datetime

        s = Source(
            type=SourceType.arxiv,
            url="https://arxiv.org/abs/1234.5678",
            published_at=datetime.now(UTC),
            raw_content_hash="abc",
        )
        assert s.reliability_prior == 0.5

    def test_reliability_out_of_range(self) -> None:
        from datetime import UTC, datetime

        with pytest.raises(ValidationError):
            Source(
                type=SourceType.arxiv,
                url="u",
                published_at=datetime.now(UTC),
                raw_content_hash="x",
                reliability_prior=1.5,
            )

    def test_author_optional(self) -> None:
        from datetime import UTC, datetime

        s = Source(
            type=SourceType.blog,
            url="https://example.com",
            published_at=datetime.now(UTC),
            raw_content_hash="y",
        )
        assert s.author is None


class TestClaimModel:
    def test_status_default(self) -> None:
        c = Claim(
            predicate="has_parameter_count",
            subject_entity_id="abc",
            object={"value": "175B"},
            source_id="src",
            extracted_by_agent="scout",
            raw_excerpt="...",
        )
        assert c.status == ClaimStatus.active

    def test_confidence_bounds(self) -> None:
        with pytest.raises(ValidationError):
            Claim(
                predicate="p",
                subject_entity_id="e",
                object={},
                source_id="s",
                extracted_by_agent="a",
                raw_excerpt="",
                confidence=1.5,
            )

    def test_superseded_by_default_none(self) -> None:
        c = Claim(
            predicate="p", subject_entity_id="e", object={},
            source_id="s", extracted_by_agent="a", raw_excerpt="",
        )
        assert c.superseded_by_claim_id is None

    @pytest.mark.parametrize(
        ("predicate", "expected"),
        [
            ("achieves_score", ClaimType.score),
            ("outperforms", ClaimType.comparison),
            ("developed_by", ClaimType.attribution),
            ("evaluated_on", ClaimType.evaluation),
            ("has_capability", ClaimType.capability),
            ("based_on", ClaimType.lineage),
            ("reproduces", ClaimType.reproduction),
            ("critiques", ClaimType.critique),
            ("speculates", ClaimType.speculative),
        ],
    )
    def test_claim_type_derived_from_predicate(
        self, predicate: str, expected: ClaimType
    ) -> None:
        c = Claim(
            predicate=predicate, subject_entity_id="e", object={},
            source_id="s", extracted_by_agent="a", raw_excerpt="",
        )
        assert c.claim_type == expected

    def test_claim_type_map_is_total_over_predicate_vocab(self) -> None:
        # The map must stay 1:1 with the extractor's predicate Literal so every
        # extracted claim lands in a real bucket (not the speculative fallback).
        from mesh_agents.claim_extractor import ExtractedClaim

        literal_predicates = set(
            ExtractedClaim.model_fields["predicate"].annotation.__args__  # type: ignore[union-attr]
        )
        assert literal_predicates == set(PREDICATE_TO_CLAIM_TYPE)

    def test_unknown_predicate_falls_back_to_speculative(self) -> None:
        assert claim_type_for_predicate("totally_unknown") == ClaimType.speculative
        c = Claim(
            predicate="totally_unknown", subject_entity_id="e", object={},
            source_id="s", extracted_by_agent="a", raw_excerpt="",
        )
        assert c.claim_type == ClaimType.speculative

    def test_explicit_claim_type_is_respected(self) -> None:
        # An explicit claim_type overrides predicate-derivation (e.g. a future
        # caller that knows better than the predicate suggests).
        c = Claim(
            predicate="achieves_score", claim_type=ClaimType.capability,
            subject_entity_id="e", object={}, source_id="s",
            extracted_by_agent="a", raw_excerpt="",
        )
        assert c.claim_type == ClaimType.capability


class TestBeliefModel:
    def test_defaults(self) -> None:
        b = Belief(topic="transformers", statement="Transformers dominate NLP")
        assert b.revision_count == 0
        assert b.is_currently_held is True
        assert b.supporting_claim_ids == []

    def test_confidence_bounds(self) -> None:
        with pytest.raises(ValidationError):
            Belief(topic="t", statement="s", confidence=-0.1)


class TestBeliefRevisionModel:
    def test_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            BeliefRevision(belief_id="b", previous_statement="old", new_statement="new")  # type: ignore[call-arg]

    def test_valid_revision(self) -> None:
        r = BeliefRevision(
            belief_id="b",
            previous_statement="old",
            new_statement="new",
            previous_confidence=0.4,
            new_confidence=0.7,
            revised_by_agent="synth",
            rationale="new evidence",
        )
        assert r.trigger_claim_ids == []


class TestRelationshipModel:
    def test_defaults(self) -> None:
        r = Relationship(from_entity_id="a", to_entity_id="b", type="cites")
        assert r.evidence_claim_ids == []
        assert r.confidence == 0.5


class TestInvestigationModel:
    def test_default_status(self) -> None:
        inv = Investigation(question="What is GPT-4's context length?")
        assert inv.status == InvestigationStatus.open

    def test_priority_bounds(self) -> None:
        with pytest.raises(ValidationError):
            Investigation(question="q", priority=2.0)

    def test_resolved_at_optional(self) -> None:
        inv = Investigation(question="q")
        assert inv.resolved_at is None
        assert inv.resolution_belief_id is None

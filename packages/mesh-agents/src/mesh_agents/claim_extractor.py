from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from mesh_llm.client import LLMResponseError, OllamaClient, OllamaNotReadyError
from mesh_llm.prompts import CLAIM_EXTRACTION_SYSTEM, format_extraction_user
from pydantic import BaseModel, Field

from mesh_agents.arxiv_scout import ScoutedPaper
from mesh_agents.base import BaseAgent

logger = logging.getLogger(__name__)


class ExtractedClaim(BaseModel):
    predicate: Literal["achieves_score", "outperforms", "developed_by", "evaluated_on"]
    subject_name: str
    object: dict[str, Any]
    raw_excerpt: str
    confidence: float = Field(default=0.85, ge=0.0, le=1.0)


class ClaimExtractionResult(BaseModel):
    claims: list[ExtractedClaim]


class ClaimExtractorInput(BaseModel):
    paper: ScoutedPaper


class ClaimExtractorOutput(BaseModel):
    claims: list[ExtractedClaim]
    entities_referenced: list[str]
    latency_ms: int = 0


def _extract_sync(llm: Any, paper: ScoutedPaper) -> tuple[list[ExtractedClaim], int]:
    user_prompt = format_extraction_user(title=paper.title, abstract=paper.abstract)
    result, latency_ms = llm.complete_with_latency(
        name="extract_claims",
        system=CLAIM_EXTRACTION_SYSTEM,
        user=user_prompt,
        response_model=ClaimExtractionResult,
    )
    return result.claims, latency_ms


class ClaimExtractorAgent(BaseAgent):
    name = "claim_extractor"

    def __init__(self, llm: OllamaClient | None = None, db_conn: Any | None = None) -> None:
        super().__init__(llm=llm, db_conn=db_conn)

    async def run(self, input: BaseModel) -> ClaimExtractorOutput:
        assert isinstance(input, ClaimExtractorInput)
        assert self.llm is not None, "ClaimExtractorAgent requires an llm client"

        try:
            claims, latency_ms = await asyncio.to_thread(_extract_sync, self.llm, input.paper)
        except OllamaNotReadyError:
            raise  # pipeline cannot proceed
        except LLMResponseError as exc:
            logger.warning(
                "claim_extraction_parse_failure",
                extra={"arxiv_id": input.paper.arxiv_id, "error": str(exc)},
            )
            return ClaimExtractorOutput(claims=[], entities_referenced=[], latency_ms=0)

        entities_referenced = list({c.subject_name for c in claims})
        return ClaimExtractorOutput(
            claims=claims,
            entities_referenced=entities_referenced,
            latency_ms=latency_ms,
        )

"""Wire models for the grounded knowledge chatbot (Phase 21).

Produced by the ResearchQA agent, returned by ``POST /api/v1/ask``, and
rendered by the wiki ``/ask`` page. Like :mod:`mesh_models.briefing`, these are
**wire-only** — no DB tables back them; Q&A answers are derived, ephemeral, and
never persisted.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

CitationKind = Literal["belief", "claim", "entity"]


class Coverage(StrEnum):
    """How well the retrieved mesh evidence supports the answer.

    Evidence-derived (the mesh's signals), never a number the model invented.
    """

    well_supported = "well_supported"
    thin = "thin"
    uncovered = "uncovered"


class Citation(BaseModel):
    """A pointer from an asserted fact to the mesh row it came from.

    ``id`` always references a row present in the retrieved context pack — the
    agent drops any id the LLM invents — so the wiki can link it to an existing
    detail page (``/knowledge/{beliefs,claims,entities}/<id>``).
    """

    kind: CitationKind
    id: str
    quote: str = ""


class Answer(BaseModel):
    """A grounded, cited answer to a single field-scoped question."""

    answer_markdown: str
    citations: list[Citation] = Field(default_factory=list)
    coverage: Coverage = Coverage.uncovered
    caveats: list[str] = Field(default_factory=list)

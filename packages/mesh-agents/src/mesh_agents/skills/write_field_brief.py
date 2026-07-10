"""Skill: ``write-field-brief`` — one succinct "state of the field" narrative.

A cooldown-gated ``stale_field_brief`` tension (one per field, due on the
maintenance timer like belief aging) routes here. The skill reads the field's
overview snapshot (strongest beliefs, movement, contested beliefs, open
discovery gaps — all via ``mesh_db.overview``), asks the LLM for a short plain
narrative grounded ONLY in that snapshot, and emits one append-only
``WriteFieldBriefEffect``. The Field Overview page serves the latest brief.

Field-agnostic by construction: the prompt is framed by the field's own
name/description from the catalog, and every fact in the narrative comes from
the snapshot. Degrades to no effect (retry next cooldown) when the LLM is
unavailable or the field holds no beliefs yet.
"""
from __future__ import annotations

import json
from typing import Any

from mesh_db.overview import field_overview_inputs
from mesh_llm.client import LLMResponseError
from mesh_llm.protocol import LLMClient
from mesh_models.effect import WriteFieldBriefEffect
from mesh_models.tension import Tension, TensionKind
from pydantic import BaseModel, Field

from mesh_agents.skill import register_skill

_AGENT = "field_brief"
_EST_COST_USD = 0.02

_SYSTEM = """\
You write the "state of the field" brief for a research knowledge base about: {field}.

You are given a JSON snapshot of what the knowledge base currently holds:
its strongest beliefs (with confidence scores), what moved recently, which
beliefs are contested by counter-evidence, and which gaps it is investigating.

Write ONE plain-prose narrative, 120-200 words, that a busy reader absorbs in
under a minute:
- Open with the field's current center of gravity (what the strongest beliefs
  collectively say).
- Note what changed in the window (new/revised/dropped beliefs) if anything did.
- Name the live disagreements (contested beliefs) plainly, without taking sides.
- Close with what remains unknown (the open gaps).

Hard rules:
1. Ground EVERY sentence in the snapshot. No outside knowledge, no invented
   facts, no hedging boilerplate.
2. No headers, bullets, or markdown — one or two short paragraphs of prose.
3. Plain words. No hype ("groundbreaking", "rapidly evolving") and no meta
   commentary about the knowledge base itself beyond what the data shows.
4. If the snapshot is thin, say what little is known in fewer words rather
   than padding.
Return only valid JSON matching the schema."""


class FieldBriefDraft(BaseModel):
    narrative: str = Field(min_length=1)


@register_skill
class WriteFieldBriefSkill:
    """Distil the field's overview snapshot → one ``WriteFieldBriefEffect``."""

    skill_id = "write-field-brief"
    handles = (TensionKind.stale_field_brief,)

    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    async def run(
        self, conn: Any, tension: Tension, *, budget_usd: float
    ) -> list[Any]:
        field_id = tension.target_ref.get("field_id") or tension.field_id
        inputs = field_overview_inputs(conn, field_id)
        if not inputs["strongest"]:
            return []  # nothing held yet — no brief to write

        llm = self._resolve_llm()
        if llm is None:
            return []  # provider unavailable — retry next cooldown

        field_label = _field_label(conn, field_id)
        try:
            draft, _latency, usage = llm.complete_with_usage(
                "write_field_brief",
                _SYSTEM.format(field=field_label),
                json.dumps(inputs, ensure_ascii=False),
                FieldBriefDraft,
            )
        except LLMResponseError:
            return []

        return [
            WriteFieldBriefEffect(
                field_id=field_id,
                narrative=draft.narrative.strip(),
                model=usage.model or getattr(llm, "model", ""),
                inputs_summary=inputs["stats"],
            )
        ]

    def _resolve_llm(self) -> LLMClient | None:
        if self._llm is not None:
            return self._llm
        try:
            from mesh_llm import make_routed_llm_client

            llm = make_routed_llm_client(agent_name=_AGENT)
            llm.health_check()
            return llm
        except Exception:
            return None


def _field_label(conn: Any, field_id: str) -> str:
    """The field's own name/description frames the prompt — nothing hardcoded."""
    from mesh_db.fields import get_field

    fld = get_field(conn, field_id)
    if fld is None:
        return field_id
    desc = getattr(fld, "description", "") or ""
    name = getattr(fld, "name", "") or field_id
    return f"{name} — {desc}" if desc else name

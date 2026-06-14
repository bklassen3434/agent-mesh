"""Render API payloads (Answer / Briefing) into plain-text Telegram messages.

Pure functions over the JSON dicts the read API returns — no ``telegram`` import,
no network — so they're trivially unit-testable. Output is plain text (no
``parse_mode``): the answer markdown can contain arbitrary characters that would
trip Telegram's strict MarkdownV2 parser, and a failed parse drops the whole
message. Plain text always delivers.
"""
from __future__ import annotations

from typing import Any

# Telegram hard-caps a message at 4096 chars; stay under it with room for a
# truncation marker.
MAX_MESSAGE_CHARS = 4000

_COVERAGE_LABEL = {
    "well_supported": "✅ well supported",
    "thin": "⚠️ thin evidence",
    "uncovered": "❓ not covered by the mesh yet",
}

_ITEM_EMOJI = {"belief": "💡", "revision": "✏️", "claim": "📄"}
_DETAIL_PATH = {"belief": "beliefs", "revision": "beliefs", "claim": "claims"}


def truncate(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def format_answer(answer: dict[str, Any]) -> str:
    """Render a ``POST /api/v1/ask`` response."""
    lines: list[str] = []
    body = (answer.get("answer_markdown") or "").strip()
    lines.append(body or "No answer.")

    coverage = answer.get("coverage")
    if coverage:
        lines.append("")
        lines.append(_COVERAGE_LABEL.get(coverage, coverage))

    caveats = answer.get("caveats") or []
    if caveats:
        lines.append("")
        lines.append("Caveats:")
        lines.extend(f"• {c}" for c in caveats)

    citations = answer.get("citations") or []
    if citations:
        refs = ", ".join(
            f"{c.get('kind', '?')}:{c.get('id', '?')}" for c in citations
        )
        lines.append("")
        lines.append(f"Sources: {refs}")

    return truncate("\n".join(lines))


def _item_line(item: dict[str, Any], wiki_url: str | None, field_slug: str) -> str:
    item_type = item.get("item_type", "item")
    emoji = _ITEM_EMOJI.get(item_type, "•")
    score = item.get("relevance_score")
    score_str = f" ({float(score):.2f})" if isinstance(score, (int, float)) else ""
    rationale = (item.get("rationale") or "").strip()
    item_id = item.get("item_id", "")

    ref = item_id
    if wiki_url and item_type in _DETAIL_PATH and item_id:
        ref = f"{wiki_url}/knowledge/{_DETAIL_PATH[item_type]}/{item_id}?field={field_slug}"

    body = rationale if rationale else f"{item_type} {item_id}"
    text = f"{emoji} {body}{score_str}"
    if ref:
        text += f"\n   {ref}"
    return text


def format_briefing(
    briefing: dict[str, Any],
    *,
    wiki_url: str | None = None,
    field_slug: str = "ai-robotics",
) -> str:
    """Render a ``GET /api/v1/briefing`` response."""
    date = briefing.get("date", "")
    lines: list[str] = [f"🗞 Daily Brief — {date}"]

    excerpt = (briefing.get("profile_excerpt") or "").strip()
    if excerpt:
        lines.append(excerpt)

    sections = briefing.get("sections") or []
    has_items = any(s.get("items") for s in sections)
    if not has_items:
        lines.append("")
        lines.append("Quiet day — nothing notable for your profile.")
        return truncate("\n".join(lines))

    for section in sections:
        items = section.get("items") or []
        if not items:
            continue
        lines.append("")
        name = section.get("name", "Items")
        lines.append(f"▸ {name}")
        for item in items:
            lines.append(_item_line(item, wiki_url, field_slug))

    return truncate("\n".join(lines))

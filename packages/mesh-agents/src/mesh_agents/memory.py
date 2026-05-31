"""Agent-side memory consumption (Phase 16).

Agents fold their own **episodic recall** (Phase 15 — what they did and how it
fared) and, once a procedural store exists, their applicable **heuristics**
(Phase 16b/d — learned how-to) into the LLM prompt as a compact, bounded text
block.

Placement rule (do not break): the block is appended to the **user** message,
*after* the ``cache_control``-marked system prefix the Anthropic client caches
(see ``mesh_llm.anthropic_client``). Only the system prompt is the cached
prefix, so per-call history/heuristics in the user message never bust the
Phase-11 prompt cache. Never inject these into the system prompt.

Reads are best-effort and read-only: the block is sourced from a ``mesh_reader``
connection (``get_connection(read_only=True)``). When no reader DSN is
configured (unit tests, minimal/local setups) or any read fails, the helpers
degrade to an empty block rather than raising — memory is soft context, never a
hard dependency of a skill.
"""
from __future__ import annotations

import logging

from mesh_db.connection import MeshConnection, get_connection
from mesh_db.episodic import EpisodicEntry, recall_history

logger = logging.getLogger(__name__)

# Default cap on entries folded into a prompt — keeps the block within a sane
# token budget regardless of how much history an agent has accumulated.
DEFAULT_RECALL_LIMIT = 10

_RECALL_HEADER = (
    "=== YOUR RECENT HISTORY (most recent first; soft context — weigh it, "
    "but never let it override the source text) ==="
)


def _open_reader() -> MeshConnection | None:
    """Best-effort read-only connection. ``None`` when no DSN is configured
    (e.g. a DB-less test) or the pool can't be reached."""
    try:
        return get_connection(read_only=True)
    except Exception:  # no DSN / unreachable pool — memory is optional
        return None


def format_episodic_block(entries: list[EpisodicEntry], limit: int = DEFAULT_RECALL_LIMIT) -> str:
    """Render episodic entries as a compact, outcome-labelled text block.

    One line per entry, newest first, each prefixed with its derived outcome
    label (``survived`` / ``contradicted`` / ``applied`` / ``held`` / …). Returns
    ``""`` for an empty list so callers can guard with a simple truthiness check
    before appending to a prompt."""
    rows = entries[:limit]
    if not rows:
        return ""
    lines = [f"- [{e.outcome.label}] {e.action_summary}" for e in rows]
    return _RECALL_HEADER + "\n" + "\n".join(lines)


def recall_block(
    agent: str,
    *,
    conn: MeshConnection | None = None,
    entity_id: str | None = None,
    source_id: str | None = None,
    topic: str | None = None,
    limit: int = DEFAULT_RECALL_LIMIT,
) -> str:
    """Episodic recall for ``agent`` at the given scope, formatted for a prompt.

    Pass ``conn`` to reuse a caller-owned connection (e.g. the sweep's); omit it
    to open and close a best-effort ``mesh_reader`` connection. Any failure (no
    DSN, unreachable DB, query error) yields ``""`` — recall is soft context."""
    owned = conn is None
    c = conn if conn is not None else _open_reader()
    if c is None:
        return ""
    try:
        entries = recall_history(
            c, agent, entity_id=entity_id, source_id=source_id, topic=topic, limit=limit
        )
    except Exception as exc:  # a memory read must never break a skill
        logger.debug("recall_block_failed", extra={"agent": agent, "error": str(exc)})
        return ""
    finally:
        if owned:
            c.close()
    return format_episodic_block(entries, limit)

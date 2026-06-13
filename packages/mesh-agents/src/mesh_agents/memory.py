"""Agent-side memory consumption (Phase 16a + 16d).

Agents fold their own **episodic recall** (Phase 15 — what they did and how it
fared) and their applicable **procedural heuristics** (Phase 16b/d — learned
how-to) into the LLM prompt as a compact, bounded text block.

Placement rule (do not break): the block is added to the **user** message,
*after* the ``cache_control``-marked system prefix the Anthropic client caches
(see ``mesh_llm.anthropic_client``). Only the system prompt is the cached
prefix, so per-call memory in the user message never busts the Phase-11 prompt
cache. Never inject these into the system prompt. The plan's ordering — applicable
heuristics, then recent history, then the task — is realized by prepending this
block to the task content within the user message.

Reads are best-effort and read-only: sourced from a ``mesh_reader`` connection
(``get_connection(read_only=True)``). When no reader DSN is configured (unit
tests, minimal/local setups) or any read fails, the helpers degrade to an empty
block rather than raising — memory is soft context, never a hard dependency.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from mesh_db.connection import MeshConnection, get_connection
from mesh_db.episodic import EpisodicEntry, recall_history
from mesh_db.heuristics import list_applicable_heuristics
from mesh_models.field import DEFAULT_FIELD_ID
from mesh_models.heuristic import AgentHeuristic

logger = logging.getLogger(__name__)

# Default caps — keep each block within a sane token budget regardless of how
# much history/how many heuristics an agent has accumulated.
DEFAULT_RECALL_LIMIT = 10
DEFAULT_HEURISTIC_LIMIT = 8

_RECALL_HEADER = (
    "=== YOUR RECENT HISTORY (most recent first; soft context — weigh it, "
    "but never let it override the source text) ==="
)
_HEURISTIC_HEADER = (
    "=== LEARNED HEURISTICS (apply where relevant; each carries a confidence "
    "it has earned over time) ==="
)


def _open_reader() -> MeshConnection | None:
    """Best-effort read-only connection. ``None`` when no DSN is configured
    (e.g. a DB-less test) or the pool can't be reached."""
    try:
        return get_connection(read_only=True)
    except Exception:  # no DSN / unreachable pool — memory is optional
        return None


def format_episodic_block(
    entries: list[EpisodicEntry], limit: int = DEFAULT_RECALL_LIMIT
) -> str:
    """Render episodic entries as a compact, outcome-labelled text block (newest
    first). Returns ``""`` for an empty list."""
    rows = entries[:limit]
    if not rows:
        return ""
    lines = [f"- [{e.outcome.label}] {e.action_summary}" for e in rows]
    return _RECALL_HEADER + "\n" + "\n".join(lines)


def format_heuristic_block(
    heuristics: list[AgentHeuristic], limit: int = DEFAULT_HEURISTIC_LIMIT
) -> str:
    """Render applicable heuristics as a compact, confidence-labelled block
    (highest confidence first). Returns ``""`` for an empty list."""
    rows = heuristics[:limit]
    if not rows:
        return ""
    lines = [f"- (confidence {h.confidence:.2f}) {h.heuristic}" for h in rows]
    return _HEURISTIC_HEADER + "\n" + "\n".join(lines)


def recall_block(
    agent: str,
    *,
    conn: MeshConnection | None = None,
    entity_id: str | None = None,
    source_id: str | None = None,
    topic: str | None = None,
    limit: int = DEFAULT_RECALL_LIMIT,
    field_id: str = DEFAULT_FIELD_ID,
) -> str:
    """Episodic recall for ``agent`` at the given scope, formatted for a prompt.
    Any failure yields ``""``. Scoped to ``field_id``."""
    owned = conn is None
    c = conn if conn is not None else _open_reader()
    if c is None:
        return ""
    try:
        entries = recall_history(
            c, agent, entity_id=entity_id, source_id=source_id, topic=topic,
            limit=limit, field_id=field_id,
        )
    except Exception as exc:  # a memory read must never break a skill
        logger.debug("recall_block_failed", extra={"agent": agent, "error": str(exc)})
        return ""
    finally:
        if owned:
            c.close()
    return format_episodic_block(entries, limit)


def build_memory_capture(
    agent: str,
    skill: str,
    *,
    conn: MeshConnection | None = None,
    entity_id: str | None = None,
    source: str | None = None,
    source_id: str | None = None,
    topic: str | None = None,
    recall_limit: int = DEFAULT_RECALL_LIMIT,
    heuristic_limit: int = DEFAULT_HEURISTIC_LIMIT,
    field_id: str = DEFAULT_FIELD_ID,
) -> tuple[str, list[str]]:
    """Like :func:`build_memory_block`, but also returns the ids of the applied
    heuristics — so an agent can report exactly which procedural memory it
    injected (Phase 23 observability) without a second read.

    Returns ``(memory_block, applied_heuristic_ids)``. Both are empty on any
    failure / no reachable DB — memory is soft context, never a hard dependency."""
    owned = conn is None
    c = conn if conn is not None else _open_reader()
    if c is None:
        return "", []
    parts: list[str] = []
    heuristic_ids: list[str] = []
    try:
        heuristics = list_applicable_heuristics(
            c, agent, skill, source=source, entity_id=entity_id,
            limit=heuristic_limit, field_id=field_id,
        )
        hblock = format_heuristic_block(heuristics, heuristic_limit)
        if hblock:
            parts.append(hblock)
            heuristic_ids = [h.id for h in heuristics[:heuristic_limit]]
        entries = recall_history(
            c, agent, entity_id=entity_id, source_id=source_id, topic=topic,
            limit=recall_limit, field_id=field_id,
        )
        rblock = format_episodic_block(entries, recall_limit)
        if rblock:
            parts.append(rblock)
    except Exception as exc:  # a memory read must never break a skill
        logger.debug("memory_block_failed", extra={"agent": agent, "error": str(exc)})
        return "", []
    finally:
        if owned:
            c.close()
    return "\n\n".join(parts), heuristic_ids


def build_memory_block(
    agent: str,
    skill: str,
    *,
    conn: MeshConnection | None = None,
    entity_id: str | None = None,
    source: str | None = None,
    source_id: str | None = None,
    topic: str | None = None,
    recall_limit: int = DEFAULT_RECALL_LIMIT,
    heuristic_limit: int = DEFAULT_HEURISTIC_LIMIT,
    field_id: str = DEFAULT_FIELD_ID,
) -> str:
    """Combined memory block for a skill: applicable (scope-matched, unexpired,
    active) heuristics first, then recent episodic history. Reads both off a
    single best-effort ``mesh_reader`` connection (or a caller-owned ``conn``).

    Heuristic scope = (agent, skill) plus optional finer ``source`` / ``entity_id``;
    expired and inactive heuristics are excluded by ``list_applicable_heuristics``.
    Episodic scope = (agent) with optional ``entity_id`` / ``source_id`` / ``topic``.
    Both reads are scoped to ``field_id`` — memory never crosses fields (17a).
    Returns ``""`` when both are empty or no DB is reachable."""
    block, _ids = build_memory_capture(
        agent, skill, conn=conn, entity_id=entity_id, source=source,
        source_id=source_id, topic=topic, recall_limit=recall_limit,
        heuristic_limit=heuristic_limit, field_id=field_id,
    )
    return block


# Reserved key under which agents attach their optional observability debug
# envelope to a skill output. The coordinator folds it into the invocation
# record (Phase 23a); any consumer that doesn't care ignores it.
DEBUG_ENVELOPE_KEY = "debug"


def debug_envelope(
    *,
    agent: str,
    memory_block: str,
    applied_heuristic_ids: list[str],
    system_prefix: str,
) -> dict[str, Any]:
    """Build the additive observability envelope an agent attaches to its skill
    output: the rendered memory block, the ids of the heuristics it applied, and
    a stable hash of the (cached) system prefix it ran under (Phase 23a)."""
    return {
        "agent": agent,
        "memory_block": memory_block,
        "applied_heuristic_ids": list(applied_heuristic_ids),
        "system_prefix_hash": hashlib.sha256(system_prefix.encode()).hexdigest(),
    }

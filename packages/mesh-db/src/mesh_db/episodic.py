"""Episodic agent read model (Phase 15a).

A *first-person, time-ordered* view of what an agent did — assembled purely
from state that is already persisted across the knowledge tables. Nothing new
is written and no new table is created; this is a read-only retrieval (mirrors
``graph.py``'s query-function style).

Episodic memory here = an agent's record of *what it did* (which claims it
extracted, which beliefs it revised), as distinct from the world-model facts
(``Claim``/``Belief``) that record what is *true about the field*. Same data,
different lens.

Reconstructability (the Phase-15 falsification question — see
``docs/episodic-memory.md``): per-agent action history is recoverable for the
agents that write *agent-attributed artifacts*:

* ``claims.extracted_by_agent`` → claim-extraction events
  (agents: ``claim_extractor``, ``skeptic``)
* ``belief_revisions.revised_by_agent`` → belief-revision events
  (agents: ``sota_tracker``, ``synthesizer``, ``skeptic``)

Two attribution facts shape this model and are deliberately surfaced rather
than papered over:

* **No ``run_id`` on artifacts.** Claims and revisions carry a timestamp but no
  run id, so each event's run is recovered by a *timestamp-containment* join to
  ``pipeline_runs`` (``started_at <= ts <= finished_at``, newest run wins). The
  coordinator owns all writes and a job does not run concurrently with itself,
  so an artifact falls inside exactly one same-type run window in practice;
  ``run_id`` is ``None`` only when no window contains the timestamp (e.g. a
  crashed run with no finalize row).
* **``skill`` is derived, not stored.** Per-artifact skill id is not persisted,
  so it is derived deterministically from ``(event_type, agent)``
  (see ``_derive_skill``).

Out of scope here (honest negatives, not bugs): ``sources`` carry no agent
attribution, so scout→source production is not per-agent reconstructable; and
``beliefs`` have no ``created_at``/``created_by``, so belief *creation* is not a
timestamped agent event (only *revisions* are). Investigations are not
agent-attributed (``assigned_scout_agents`` is never populated) — they surface
as an outcome dimension in Phase 15b, not as a standalone event source.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from mesh_db.connection import MeshConnection

# Hard cap on returned entries, matching the access layer's other read modules.
MAX_LIMIT = 200

# Agents that write each kind of agent-attributed artifact. Used only for
# documentation / validation; the queries filter by the requested agent.
EXTRACTION_AGENTS = ("claim_extractor", "skeptic")
REVISION_AGENTS = ("sota_tracker", "synthesizer", "skeptic")


class EpisodicEntry(BaseModel):
    """One first-person action an agent took, reconstructed from existing state."""

    run_id: str | None
    timestamp: datetime
    agent: str
    skill: str
    event_type: str  # "extraction" | "belief_revision"
    action_summary: str
    refs: dict[str, Any] = Field(default_factory=dict)


# Per-artifact skill id is not persisted; derive it deterministically from the
# event type + author. Falls back to the agent name for any unmapped author so a
# new writer never produces a null skill.
_SKILL_MAP: dict[tuple[str, str], str] = {
    ("extraction", "claim_extractor"): "extract_claims",
    ("extraction", "skeptic"): "challenge_belief",
    ("belief_revision", "sota_tracker"): "update_sota",
    ("belief_revision", "synthesizer"): "synthesize_capability",
    ("belief_revision", "skeptic"): "challenge_belief",
}


def _derive_skill(event_type: str, agent: str) -> str:
    return _SKILL_MAP.get((event_type, agent), agent)


# Newest pipeline_runs row whose [started_at, finished_at] window contains the
# artifact timestamp. Inlined as a correlated subquery on each branch (the
# artifact tables have no run_id). An open-ended (finished_at IS NULL) run
# matches anything at/after its start — covers a crashed run that never
# finalized.
_CONTAINING_RUN = """
    (SELECT pr.id FROM pipeline_runs pr
     WHERE pr.started_at <= {ts}
       AND (pr.finished_at IS NULL OR {ts} <= pr.finished_at)
     ORDER BY pr.started_at DESC
     LIMIT 1)
"""


def _extraction_events(
    conn: MeshConnection,
    agent: str,
    entity_id: str | None,
    source_id: str | None,
    since: datetime | None,
    until: datetime | None,
    limit: int,
) -> list[EpisodicEntry]:
    """Claim-extraction events: claims this agent produced, grouped per source
    per run (one event = "extracted N claims from source X during run R")."""
    conds = ["c.extracted_by_agent = %(agent)s"]
    params: dict[str, Any] = {"agent": agent, "limit": limit}
    if entity_id is not None:
        conds.append("c.subject_entity_id = %(entity_id)s")
        params["entity_id"] = entity_id
    if source_id is not None:
        conds.append("c.source_id = %(source_id)s")
        params["source_id"] = source_id
    if since is not None:
        conds.append("c.extracted_at >= %(since)s")
        params["since"] = since
    if until is not None:
        conds.append("c.extracted_at <= %(until)s")
        params["until"] = until

    run = _CONTAINING_RUN.format(ts="c.extracted_at")
    sql = f"""
        WITH ev AS (
            SELECT
                c.id AS claim_id,
                c.source_id,
                c.subject_entity_id,
                c.extracted_at,
                {run} AS run_id
            FROM claims c
            WHERE {" AND ".join(conds)}
        )
        SELECT
            ev.run_id,
            ev.source_id,
            s.type AS source_type,
            s.url  AS source_url,
            MAX(ev.extracted_at) AS ts,
            COUNT(*) AS n,
            array_agg(ev.claim_id ORDER BY ev.extracted_at) AS claim_ids,
            array_agg(DISTINCT ev.subject_entity_id) AS entity_ids
        FROM ev
        LEFT JOIN sources s ON s.id = ev.source_id
        GROUP BY ev.run_id, ev.source_id, s.type, s.url
        ORDER BY ts DESC
        LIMIT %(limit)s
    """
    rows = conn.execute(sql, params).fetchall()
    entries: list[EpisodicEntry] = []
    for r in rows:
        run_id, src_id, src_type, src_url, ts, n, claim_ids, entity_ids = r[:8]
        label = src_type or "source"
        where = f" ({src_url})" if src_url else f" {src_id}"
        entries.append(
            EpisodicEntry(
                run_id=None if run_id is None else str(run_id),
                timestamp=ts,
                agent=agent,
                skill=_derive_skill("extraction", agent),
                event_type="extraction",
                action_summary=f"Extracted {int(n)} claim(s) from {label}{where}",
                refs={
                    "source_id": str(src_id),
                    "claim_ids": [str(x) for x in (claim_ids or [])],
                    "entity_ids": [str(x) for x in (entity_ids or [])],
                },
            )
        )
    return entries


def _revision_events(
    conn: MeshConnection,
    agent: str,
    entity_id: str | None,
    topic: str | None,
    since: datetime | None,
    until: datetime | None,
    limit: int,
) -> list[EpisodicEntry]:
    """Belief-revision events: one per ``belief_revisions`` row this agent wrote."""
    conds = ["br.revised_by_agent = %(agent)s"]
    params: dict[str, Any] = {"agent": agent, "limit": limit}
    if topic is not None:
        conds.append("b.topic ILIKE %(topic)s")
        params["topic"] = f"%{topic}%"
    if entity_id is not None:
        # A revision is "about" an entity when it revises that entity's
        # capability belief, or when one of its trigger claims has that entity
        # as subject.
        conds.append(
            "(b.topic = 'capability:' || %(entity_id)s"
            " OR EXISTS (SELECT 1 FROM claims c"
            "            WHERE c.id = ANY(br.trigger_claim_ids)"
            "              AND c.subject_entity_id = %(entity_id)s))"
        )
        params["entity_id"] = entity_id
    if since is not None:
        conds.append("br.revised_at >= %(since)s")
        params["since"] = since
    if until is not None:
        conds.append("br.revised_at <= %(until)s")
        params["until"] = until

    run = _CONTAINING_RUN.format(ts="br.revised_at")
    sql = f"""
        SELECT
            br.revised_at AS ts,
            br.belief_id,
            b.topic,
            br.trigger_claim_ids,
            (br.new_confidence - br.previous_confidence) AS conf_delta,
            (br.previous_statement IS DISTINCT FROM br.new_statement) AS stmt_changed,
            {run} AS run_id
        FROM belief_revisions br
        JOIN beliefs b ON b.id = br.belief_id
        WHERE {" AND ".join(conds)}
        ORDER BY ts DESC
        LIMIT %(limit)s
    """
    rows = conn.execute(sql, params).fetchall()
    entries: list[EpisodicEntry] = []
    for r in rows:
        ts, belief_id, btopic, trigger_ids, conf_delta, stmt_changed, run_id = r[:7]
        verb = "Challenged" if agent == "skeptic" else (
            "Revised" if stmt_changed else "Adjusted confidence on"
        )
        entries.append(
            EpisodicEntry(
                run_id=None if run_id is None else str(run_id),
                timestamp=ts,
                agent=agent,
                skill=_derive_skill("belief_revision", agent),
                event_type="belief_revision",
                action_summary=(
                    f"{verb} belief '{btopic}' (confidence {float(conf_delta):+.2f})"
                ),
                refs={
                    "belief_id": str(belief_id),
                    "topic": str(btopic),
                    "trigger_claim_ids": [str(x) for x in (trigger_ids or [])],
                },
            )
        )
    return entries


def recall_history(
    conn: MeshConnection,
    agent: str,
    *,
    entity_id: str | None = None,
    source_id: str | None = None,
    topic: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 50,
) -> list[EpisodicEntry]:
    """Time-ordered (most-recent-first) episodic action history for ``agent``.

    Merges the agent-attributed event sources (claim extraction + belief
    revision), each with its run recovered by timestamp containment. Scope
    filters are applied per source where they are meaningful:

    * ``entity_id`` — extraction events on that subject entity; revision events
      on that entity's capability belief or whose trigger claims are about it.
    * ``source_id`` — extraction events from that source. Revision events have
      no source, so they are excluded when ``source_id`` is given.
    * ``topic``     — substring match on belief topic (revision events only).
      Extraction events have no topic, so they are excluded when ``topic`` is
      given.
    * ``since`` / ``until`` — inclusive timestamp window.
    * ``limit`` — capped at ``MAX_LIMIT``.

    Read-only: issues only SELECTs; never writes.
    """
    limit = min(max(limit, 0), MAX_LIMIT)
    if limit == 0:
        return []

    entries: list[EpisodicEntry] = []
    # Extraction events have no topic; skip that source when a topic filter is set.
    if topic is None:
        entries.extend(
            _extraction_events(conn, agent, entity_id, source_id, since, until, limit)
        )
    # Revision events have no source; skip that source when a source filter is set.
    if source_id is None:
        entries.extend(
            _revision_events(conn, agent, entity_id, topic, since, until, limit)
        )

    entries.sort(key=lambda e: e.timestamp, reverse=True)
    return entries[:limit]

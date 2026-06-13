"""Episodic agent read model (Phase 15).

A *first-person, time-ordered* view of what an agent did — assembled purely
from state that is already persisted across the knowledge tables, and (15b)
labelled with what became of that work. Nothing new is written and no new table
is created; this is a read-only retrieval (mirrors ``graph.py``'s
query-function style).

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
only as an *outcome* dimension (15b), linked to the extraction events whose
claims they collected.

Phase 15b — outcome tagging. Each entry carries a deterministic ``outcome``
computed purely in SQL from current table state (no LLM, no new columns):

* Extraction events: the fate of the claims produced — promoted into a held
  belief (supporting), contradicted (their belief drew skeptic counter-claims),
  applied as contradicting evidence (skeptic counter-claims), superseded — plus
  any ``failure_mode``s and the status of investigations they were collected
  into (resolved / abandoned / open).
* Belief-revision events: whether the belief is still held, and how many
  revisions superseded this one.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from mesh_models.field import DEFAULT_FIELD_ID
from pydantic import BaseModel, Field

from mesh_db.connection import MeshConnection

# Hard cap on returned entries, matching the access layer's other read modules.
MAX_LIMIT = 200

# Agents that write each kind of agent-attributed artifact. Used only for
# documentation / validation; the queries filter by the requested agent.
EXTRACTION_AGENTS = ("claim_extractor", "skeptic")
REVISION_AGENTS = ("sota_tracker", "synthesizer", "skeptic")


class EpisodicOutcome(BaseModel):
    """What became of an episodic action, derived deterministically (15b)."""

    label: str
    # Extraction-event facets (claim fates).
    claims_total: int = 0
    claims_supporting: int = 0  # promoted into a currently-held belief
    claims_contradicting: int = 0  # applied as contradicting evidence (skeptic)
    claims_contested: int = 0  # supporting a belief that drew contradicting claims
    claims_superseded: int = 0
    failure_modes: list[str] = Field(default_factory=list)
    investigations: dict[str, int] = Field(default_factory=dict)  # status -> count
    # Revision-event facets.
    belief_currently_held: bool | None = None
    later_revisions: int = 0


class EpisodicEntry(BaseModel):
    """One first-person action an agent took, reconstructed from existing state."""

    run_id: str | None
    timestamp: datetime
    agent: str
    skill: str
    event_type: str  # "extraction" | "belief_revision"
    action_summary: str
    refs: dict[str, Any] = Field(default_factory=dict)
    outcome: EpisodicOutcome


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
       AND pr.field_id = %(field_id)s
     ORDER BY pr.started_at DESC
     LIMIT 1)
"""


def _extraction_label(agent: str, o: EpisodicOutcome) -> str:
    """Headline outcome for an extraction event, from the claim-fate facets."""
    if agent == "skeptic":
        # Skeptic counter-claims: "applied" once attached as contradicting
        # evidence (verdict=contradicted); a weakened verdict creates them
        # without attaching, so they remain "unused".
        return "applied" if o.claims_contradicting > 0 else "unused"
    if o.claims_contested > 0:
        return "contradicted"  # supported a belief that later drew counter-claims
    if o.claims_superseded == o.claims_total and o.claims_total > 0:
        return "superseded"
    if o.claims_supporting > 0:
        return "survived"  # promoted into a held belief, not contradicted
    return "pending"


def _investigation_status_by_claim(
    conn: MeshConnection, claim_ids: list[str]
) -> dict[str, list[str]]:
    """claim_id -> investigation statuses, for claims collected into any
    investigation. One query over the relevant claim set (read-only)."""
    if not claim_ids:
        return {}
    rows = conn.execute(
        """
        SELECT ci AS claim_id, i.status
        FROM investigations i, unnest(i.collected_claim_ids) AS ci
        WHERE ci = ANY(%(claim_ids)s)
        """,
        {"claim_ids": claim_ids},
    ).fetchall()
    out: dict[str, list[str]] = {}
    for claim_id, status in rows:
        out.setdefault(str(claim_id), []).append(str(status))
    return out


def _extraction_events(
    conn: MeshConnection,
    agent: str,
    entity_id: str | None,
    source_id: str | None,
    since: datetime | None,
    until: datetime | None,
    limit: int,
    field_id: str,
) -> list[EpisodicEntry]:
    """Claim-extraction events: claims this agent produced, grouped per source
    per run (one event = "extracted N claims from source X during run R"),
    each tagged with the aggregate fate of those claims."""
    conds = ["c.extracted_by_agent = %(agent)s", "c.field_id = %(field_id)s"]
    params: dict[str, Any] = {"agent": agent, "limit": limit, "field_id": field_id}
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
    # Per-claim belief-role facets (15b): is the claim promoted as supporting
    # evidence in a held belief, applied as contradicting evidence, or supporting
    # a belief that itself drew contradicting claims. Computed once in the CTE,
    # then rolled up with FILTER aggregates per event.
    sql = f"""
        WITH ev AS (
            SELECT
                c.id AS claim_id,
                c.source_id,
                c.subject_entity_id,
                c.extracted_at,
                c.status,
                c.failure_mode,
                EXISTS (SELECT 1 FROM beliefs b WHERE b.is_currently_held
                        AND c.id = ANY(b.supporting_claim_ids)) AS is_supporting,
                EXISTS (SELECT 1 FROM beliefs b WHERE b.is_currently_held
                        AND c.id = ANY(b.contradicting_claim_ids)) AS is_contradicting,
                EXISTS (SELECT 1 FROM beliefs b WHERE b.is_currently_held
                        AND c.id = ANY(b.supporting_claim_ids)
                        AND cardinality(b.contradicting_claim_ids) > 0)
                    AS supports_attacked,
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
            array_agg(DISTINCT ev.subject_entity_id) AS entity_ids,
            COUNT(*) FILTER (WHERE ev.is_supporting) AS n_supporting,
            COUNT(*) FILTER (WHERE ev.is_contradicting) AS n_contradicting,
            COUNT(*) FILTER (WHERE ev.supports_attacked) AS n_contested,
            COUNT(*) FILTER (WHERE ev.status = 'superseded') AS n_superseded,
            array_remove(array_agg(DISTINCT ev.failure_mode), NULL) AS failure_modes
        FROM ev
        LEFT JOIN sources s ON s.id = ev.source_id
        GROUP BY ev.run_id, ev.source_id, s.type, s.url
        ORDER BY ts DESC
        LIMIT %(limit)s
    """
    rows = conn.execute(sql, params).fetchall()

    # Investigation fates of the produced claims, in one extra query over the
    # union of this page's claim ids (investigations are not agent-attributed, so
    # they attach to extraction events via collected_claim_ids).
    all_claim_ids = [str(x) for r in rows for x in (r[6] or [])]
    inv_by_claim = _investigation_status_by_claim(conn, all_claim_ids)

    entries: list[EpisodicEntry] = []
    for r in rows:
        (
            run_id, src_id, src_type, src_url, ts, n, claim_ids, entity_ids,
            n_supporting, n_contradicting, n_contested, n_superseded, failure_modes,
        ) = r[:13]
        claim_id_list = [str(x) for x in (claim_ids or [])]
        inv_counts: dict[str, int] = {}
        for cid in claim_id_list:
            for st in inv_by_claim.get(cid, []):
                inv_counts[st] = inv_counts.get(st, 0) + 1
        outcome = EpisodicOutcome(
            label="",  # set below once facets are populated
            claims_total=int(n),
            claims_supporting=int(n_supporting),
            claims_contradicting=int(n_contradicting),
            claims_contested=int(n_contested),
            claims_superseded=int(n_superseded),
            failure_modes=[str(f) for f in (failure_modes or [])],
            investigations=inv_counts,
        )
        outcome.label = _extraction_label(agent, outcome)
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
                    "claim_ids": claim_id_list,
                    "entity_ids": [str(x) for x in (entity_ids or [])],
                },
                outcome=outcome,
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
    field_id: str,
) -> list[EpisodicEntry]:
    """Belief-revision events: one per ``belief_revisions`` row this agent wrote,
    tagged with the belief's current fate (still held, and how many later
    revisions superseded this one)."""
    conds = ["br.revised_by_agent = %(agent)s", "b.field_id = %(field_id)s"]
    params: dict[str, Any] = {"agent": agent, "limit": limit, "field_id": field_id}
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
            b.is_currently_held,
            br.trigger_claim_ids,
            (br.new_confidence - br.previous_confidence) AS conf_delta,
            (br.previous_statement IS DISTINCT FROM br.new_statement) AS stmt_changed,
            (SELECT COUNT(*) FROM belief_revisions br2
             WHERE br2.belief_id = br.belief_id
               AND br2.revised_at > br.revised_at) AS later_revisions,
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
        (
            ts, belief_id, btopic, held, trigger_ids, conf_delta,
            stmt_changed, later_revisions, run_id,
        ) = r[:9]
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
                outcome=EpisodicOutcome(
                    label="held" if held else "retired",
                    belief_currently_held=bool(held),
                    later_revisions=int(later_revisions),
                ),
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
    field_id: str = DEFAULT_FIELD_ID,
) -> list[EpisodicEntry]:
    """Time-ordered (most-recent-first) episodic action history for ``agent``,
    each entry tagged with its derived outcome (15b). Scoped to ``field_id`` —
    an agent's history never crosses fields (Phase 17a).

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
            _extraction_events(
                conn, agent, entity_id, source_id, since, until, limit, field_id
            )
        )
    # Revision events have no source; skip that source when a source filter is set.
    if source_id is None:
        entries.extend(
            _revision_events(
                conn, agent, entity_id, topic, since, until, limit, field_id
            )
        )

    entries.sort(key=lambda e: e.timestamp, reverse=True)
    return entries[:limit]

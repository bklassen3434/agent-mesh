"""Belief-consolidation sweep engine (Phase 19d/19e).

The world-model analog of ``mesh_agents.reconcile``: blocks → matches → merges
the currently-held belief corpus of one field, folding semantic duplicates onto
a canonical belief, then ages stale beliefs (decay + archival). Middle-band
adjudications route through the Anthropic Batch API (50% cheaper) when the client
supports it, falling back to synchronous calls otherwise.

Append-only and conservative: merge marks the absorbed belief not-held (never
deletes), decay only lowers confidence, archival only flips ``is_currently_held``
— every change records a ``BeliefRevision`` attributed to ``belief_consolidator``.
Field-scoped throughout; never compares or merges across fields.

The pure pieces here (candidate loading, blocking+banding, cluster+merge, decay,
archival) back the controller's consolidation: ``plan_decay_and_archive`` feeds
the ``maintain-belief`` skill, and merge banding feeds ``consolidate-beliefs``.
This module also exposes the synchronous ``reconcile_beliefs`` entry the one-time
CLI backfill uses (mirroring how ``reconcile_entities`` backs
``mesh.cli reconcile-entities``).
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from mesh_db.beliefs import (
    ConfidenceFn,
    belief_family,
    choose_canonical_belief,
    find_candidate_duplicate_beliefs,
    get_belief_by_id,
    list_beliefs,
    merge_beliefs,
    set_belief_embedding,
    update_belief,
)
from mesh_db.claims import get_claims_by_ids
from mesh_db.connection import MeshConnection
from mesh_db.revisions import create_revision
from mesh_llm.embeddings import Embedder, belief_embed_text
from mesh_models.belief import Belief
from mesh_models.claim import ClaimStatus
from mesh_models.field import DEFAULT_FIELD_ID
from mesh_models.revision import BeliefRevision

from mesh_agents.belief_consolidation import (
    BeliefForMatch,
    BeliefMatchDecision,
    BeliefMergeConfig,
    adjudicate_beliefs,
    band,
    belief_for_match,
    build_belief_adjudication_batch_items,
    make_confidence_fn,
)
from mesh_agents.confidence import ConfidenceWeights

_AGENT = "belief_consolidator"


class _BatchLLM(Protocol):
    def submit_batch(self, items: list[Any], response_model: type) -> str: ...
    def batch_status(self, batch_id: str) -> str: ...
    def collect_batch(self, batch_id: str, response_model: type) -> dict[str, Any]: ...


# ── env knobs ────────────────────────────────────────────────────────────────


def candidate_limit() -> int:
    """Cap on query beliefs scanned per field per run (incrementality bound).
    The most-recently-revised held beliefs are scanned first."""
    return int(os.environ.get("MESH_BELIEF_CANDIDATE_LIMIT", "500"))


def decay_halflife_days() -> float:
    return float(os.environ.get("MESH_BELIEF_DECAY_HALFLIFE_DAYS", "90"))


def decay_floor() -> float:
    return float(os.environ.get("MESH_BELIEF_DECAY_FLOOR", "0.1"))


def archive_after_days() -> int:
    return int(os.environ.get("MESH_BELIEF_ARCHIVE_AFTER_DAYS", "365"))


# ── report ───────────────────────────────────────────────────────────────────


@dataclass
class BeliefMergeRecord:
    canonical_id: str
    canonical_topic: str
    absorbed: list[tuple[str, str]]  # (id, topic)


@dataclass
class BeliefConsolidationReport:
    beliefs_held_before: int = 0
    beliefs_held_after: int = 0
    embedded_now: int = 0
    auto_merges: int = 0       # high-band pairs
    adjudications: int = 0     # middle-band pairs sent to the LLM
    merges: int = 0            # beliefs absorbed (duplicates folded away)
    decayed: int = 0
    archived: int = 0
    dry_run: bool = False
    merge_records: list[BeliefMergeRecord] = field(default_factory=list)


# ── union-find (cluster confirmed pairs) ──────────────────────────────────────


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self._parent.setdefault(x, x)
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb

    def clusters(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for node in self._parent:
            out.setdefault(self.find(node), []).append(node)
        return {root: members for root, members in out.items() if len(members) > 1}


# ── shared steps (used by both the CLI engine and the LangGraph job) ──────────


def ensure_belief_embeddings(
    conn: MeshConnection, embedder: Embedder, field_id: str = DEFAULT_FIELD_ID
) -> int:
    """Backfill ``statement_embedding`` for every currently-held belief in the
    field that lacks one, so blocking sees the whole held set. Returns the count
    persisted. Mirrors ``reconcile._ensure_embeddings``."""
    rows = conn.execute(
        "SELECT id, topic, statement FROM beliefs "
        "WHERE is_currently_held = TRUE AND field_id = %s "
        "AND statement_embedding IS NULL",
        [field_id],
    ).fetchall()
    if not rows:
        return 0
    vectors = embedder.embed([belief_embed_text(str(r[1]), str(r[2])) for r in rows])
    for (bid, _topic, _stmt), vec in zip(rows, vectors, strict=True):
        set_belief_embedding(conn, str(bid), vec)
    return len(rows)


def load_candidate_beliefs(
    conn: MeshConnection, field_id: str = DEFAULT_FIELD_ID
) -> tuple[list[Belief], int]:
    """The query beliefs to scan this run: currently-held, most-recently-revised
    first, capped at ``candidate_limit()``. Returns ``(candidates, total_held)``
    so the caller can log how many were skipped (the incrementality bound)."""
    total_held = len(list_beliefs(conn, currently_held=True, limit=100000, field_id=field_id))
    candidates = list_beliefs(
        conn, currently_held=True, limit=candidate_limit(), field_id=field_id
    )
    return candidates, total_held


@dataclass(frozen=True)
class _PairEndpoints:
    """A middle-band pair awaiting adjudication, with both endpoints' text."""

    a_id: str
    b_id: str
    a: BeliefForMatch
    b: BeliefForMatch


def block_and_band(
    conn: MeshConnection,
    embedder: Embedder,
    candidates: list[Belief],
    *,
    config: BeliefMergeConfig,
    k: int = 10,
    field_id: str = DEFAULT_FIELD_ID,
) -> tuple[set[frozenset[str]], dict[frozenset[str], _PairEndpoints]]:
    """Block each candidate against held same-family beliefs and band the
    neighbours. Returns ``(confirmed_high_pairs, middle_pairs_by_key)``. The
    middle map carries both endpoints' text so adjudication (sync or batch) needs
    no re-read."""
    confirmed: set[frozenset[str]] = set()
    middle: dict[frozenset[str], _PairEndpoints] = {}
    for cand in candidates:
        vec = embedder.embed([belief_embed_text(cand.topic, cand.statement)])[0]
        family = belief_family(cand.topic)
        for nb_id, nb_topic, nb_statement, distance in find_candidate_duplicate_beliefs(
            conn, vec, exclude_id=cand.id, k=k, field_id=field_id, family=family
        ):
            pair = frozenset({cand.id, nb_id})
            if len(pair) < 2:
                continue
            decision = band(1.0 - distance, config)
            if decision == "merge":
                confirmed.add(pair)
                middle.pop(pair, None)
            elif decision == "adjudicate" and pair not in confirmed:
                middle.setdefault(
                    pair,
                    _PairEndpoints(
                        a_id=cand.id,
                        b_id=nb_id,
                        a=belief_for_match(cand),
                        b=BeliefForMatch(topic=nb_topic, statement=nb_statement),
                    ),
                )
    return confirmed, middle


def adjudicate_middle_pairs(
    llm: Any | None,
    middle: dict[frozenset[str], _PairEndpoints],
    *,
    batch_poll_seconds: float = 10.0,
) -> set[frozenset[str]]:
    """Resolve middle-band pairs to confirmed-same via the LLM (batch when the
    client supports it, else synchronous). Defaults every pair to not-same when
    ``llm`` is None (conservative). Mirrors ``reconcile._adjudicate``."""
    confirmed: set[frozenset[str]] = set()
    if llm is None or not middle:
        return confirmed
    ordered = list(middle.items())
    if hasattr(llm, "submit_batch"):
        batch_llm: _BatchLLM = llm
        items = build_belief_adjudication_batch_items(
            [(f"{ep.a_id}|{ep.b_id}", ep.a, ep.b) for _key, ep in ordered]
        )
        batch_id = batch_llm.submit_batch(items, BeliefMatchDecision)
        while batch_llm.batch_status(batch_id) != "ended":
            time.sleep(batch_poll_seconds)
        results = batch_llm.collect_batch(batch_id, BeliefMatchDecision)
        for key, ep in ordered:
            parsed = getattr(results.get(f"{ep.a_id}|{ep.b_id}"), "parsed", None)
            if isinstance(parsed, BeliefMatchDecision) and parsed.same_proposition:
                confirmed.add(key)
    else:
        for key, ep in ordered:
            if adjudicate_beliefs(llm, ep.a, ep.b):
                confirmed.add(key)
    return confirmed


def cluster_and_merge(
    conn: MeshConnection,
    confirmed: set[frozenset[str]],
    *,
    confidence_fn: ConfidenceFn,
    dry_run: bool,
) -> list[BeliefMergeRecord]:
    """Cluster confirmed-same pairs (transitive closure) and fold each cluster
    onto one canonical via ``merge_beliefs``. Re-confirms the canonical direction
    per absorbed pair (claim counts shift as earlier merges in the cluster accrue
    onto the canonical). Returns one record per cluster."""
    uf = _UnionFind()
    for pair in confirmed:
        a, b = sorted(pair)
        uf.union(a, b)

    records: list[BeliefMergeRecord] = []
    for member_ids in uf.clusters().values():
        canonical = _pick_cluster_canonical(conn, member_ids)
        absorbed_ids = [m for m in member_ids if m != canonical]
        canonical_b = get_belief_by_id(conn, canonical)
        canonical_topic = canonical_b.topic if canonical_b else canonical
        absorbed: list[tuple[str, str]] = []
        for dup in absorbed_ids:
            dup_b = get_belief_by_id(conn, dup)
            absorbed.append((dup, dup_b.topic if dup_b else dup))
        records.append(
            BeliefMergeRecord(
                canonical_id=canonical,
                canonical_topic=canonical_topic,
                absorbed=absorbed,
            )
        )
        if not dry_run:
            for dup in absorbed_ids:
                c, d = choose_canonical_belief(conn, canonical, dup)
                merge_beliefs(conn, c, d, confidence_fn=confidence_fn)
    return records


def _pick_cluster_canonical(conn: MeshConnection, member_ids: list[str]) -> str:
    """Generalize ``choose_canonical_belief`` to a cluster: more supporting
    claims → higher revision_count → earliest last_revised_at → smallest id."""
    best: tuple[int, int, datetime, str] | None = None
    best_id = member_ids[0]
    for mid in member_ids:
        b = get_belief_by_id(conn, mid)
        if b is None:
            continue
        key = (
            -len(b.supporting_claim_ids),
            -b.revision_count,
            b.last_revised_at,
            mid,
        )
        if best is None or key < best:
            best = key
            best_id = mid
    return best_id


# ── staleness decay + archival (Phase 19e) — LLM-free ─────────────────────────


def _has_live_supporting_claim(conn: MeshConnection, belief: Belief) -> bool:
    """True if any of the belief's supporting claims is still ``active`` (live
    evidence). A belief with no supporting claims has no live evidence."""
    if not belief.supporting_claim_ids:
        return False
    claims = get_claims_by_ids(conn, list(belief.supporting_claim_ids))
    return any(c.status == ClaimStatus.active for c in claims)


@dataclass(frozen=True)
class DecayDecision:
    """One LLM-free aging action on a held belief: decay its confidence or archive
    it. ``archive=True`` flips it out of the held set; otherwise it is a decay
    (confidence lowered, statement unchanged). Pure data — the applier (or the
    controller's ``maintain-belief`` skill, via a ``ReviseBeliefEffect``) performs
    the append-only write."""

    belief_id: str
    statement: str  # unchanged — decay/archival never rewrite the statement
    previous_confidence: float
    new_confidence: float
    archive: bool
    rationale: str


def plan_decay_and_archive(
    conn: MeshConnection,
    *,
    now: datetime | None = None,
    field_id: str = DEFAULT_FIELD_ID,
) -> list[DecayDecision]:
    """Decide the held corpus's aging actions WITHOUT writing (LLM-free).

    Archive: a held belief not revised for longer than ``archive_after_days`` AND
    unsupported by any live claim drops out of the held set. Decay: a held belief
    older than the half-life has its confidence multiplied by
    ``0.5 ** (age / halflife)``, floored at ``decay_floor()`` — emitted only when
    that actually lowers it (so floor-pinned beliefs produce no action). Archival
    takes precedence over decay for the same belief. Field-scoped."""
    now = now or datetime.now(UTC)
    halflife = decay_halflife_days()
    floor = decay_floor()
    archive_cutoff = now - timedelta(days=archive_after_days())
    decay_cutoff = now - timedelta(days=halflife)

    held = list_beliefs(conn, currently_held=True, limit=100000, field_id=field_id)
    out: list[DecayDecision] = []
    for b in held:
        age_days = (now - b.last_revised_at).total_seconds() / 86400.0
        if b.last_revised_at < archive_cutoff and not _has_live_supporting_claim(conn, b):
            out.append(
                DecayDecision(
                    belief_id=b.id,
                    statement=b.statement,
                    previous_confidence=b.confidence,
                    new_confidence=b.confidence,
                    archive=True,
                    rationale="archived: stale, no live evidence",
                )
            )
            continue
        if b.last_revised_at < decay_cutoff:
            factor = 0.5 ** (age_days / halflife) if halflife > 0 else 1.0
            new_conf = max(floor, b.confidence * factor)
            if not math.isclose(new_conf, b.confidence, abs_tol=1e-6) and new_conf < b.confidence:
                out.append(
                    DecayDecision(
                        belief_id=b.id,
                        statement=b.statement,
                        previous_confidence=b.confidence,
                        new_confidence=new_conf,
                        archive=False,
                        rationale="staleness decay",
                    )
                )
    return out


def decay_and_archive(
    conn: MeshConnection,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    field_id: str = DEFAULT_FIELD_ID,
) -> tuple[int, int]:
    """Age the held corpus (LLM-free). Returns ``(decayed, archived)``.

    The standalone-sweep applier: plan the actions then write each as an
    append-only revision (no row deleted). The controller's ``maintain-belief``
    skill reuses ``plan_decay_and_archive`` but routes the same decisions through
    the write gateway as ``ReviseBeliefEffect``s instead."""
    now = now or datetime.now(UTC)
    decisions = plan_decay_and_archive(conn, now=now, field_id=field_id)
    decayed = sum(1 for d in decisions if not d.archive)
    archived = sum(1 for d in decisions if d.archive)
    if not dry_run:
        for d in decisions:
            _record_decay(conn, d, now)
    return decayed, archived


def _record_decay(conn: MeshConnection, decision: DecayDecision, now: datetime) -> None:
    """Append a decay/archive revision (statement unchanged) and update the
    belief. Update-before-revision (FK ordering, like merge_beliefs)."""
    existing = get_belief_by_id(conn, decision.belief_id)
    if existing is None:
        return
    update_fields: dict[str, Any] = {
        "confidence": decision.new_confidence,
        "last_revised_at": now,
        "revision_count": existing.revision_count + 1,
    }
    if decision.archive:
        update_fields["is_currently_held"] = False
    update_belief(conn, decision.belief_id, **update_fields)
    create_revision(
        conn,
        BeliefRevision(
            belief_id=decision.belief_id,
            previous_statement=existing.statement,
            new_statement=existing.statement,
            previous_confidence=existing.confidence,
            new_confidence=decision.new_confidence,
            trigger_claim_ids=[],
            revised_at=now,
            revised_by_agent=_AGENT,
            rationale=decision.rationale,
        ),
    )


# ── synchronous reconcile (one-time CLI backfill) ─────────────────────────────


def reconcile_beliefs(
    conn: MeshConnection,
    embedder: Embedder,
    llm: Any | None = None,
    *,
    config: BeliefMergeConfig | None = None,
    weights: ConfidenceWeights | None = None,
    k: int = 10,
    dry_run: bool = False,
    decay: bool = True,
    batch_poll_seconds: float = 10.0,
    field_id: str = DEFAULT_FIELD_ID,
) -> BeliefConsolidationReport:
    """One-pass belief consolidation over one field's held corpus: backfill
    embeddings → block+band → adjudicate middle (batch/sync) → cluster+merge →
    (optionally) decay+archive. Synchronous; mirrors ``reconcile_entities``. The
    CLI ``consolidate-beliefs`` calls this; the scheduled job uses the shared
    steps directly so it can stream cost to Langfuse."""
    cfg = config or BeliefMergeConfig.from_env()
    report = BeliefConsolidationReport(dry_run=dry_run)
    confidence_fn = make_confidence_fn(weights)

    report.embedded_now = ensure_belief_embeddings(conn, embedder, field_id)
    candidates, total_held = load_candidate_beliefs(conn, field_id)
    report.beliefs_held_before = total_held
    if not candidates:
        report.beliefs_held_after = total_held
        return report

    confirmed, middle = block_and_band(
        conn, embedder, candidates, config=cfg, k=k, field_id=field_id
    )
    report.auto_merges = len(confirmed)
    report.adjudications = len(middle)
    confirmed |= adjudicate_middle_pairs(
        llm, middle, batch_poll_seconds=batch_poll_seconds
    )

    report.merge_records = cluster_and_merge(
        conn, confirmed, confidence_fn=confidence_fn, dry_run=dry_run
    )
    report.merges = sum(len(r.absorbed) for r in report.merge_records)

    if decay:
        report.decayed, report.archived = decay_and_archive(
            conn, dry_run=dry_run, field_id=field_id
        )

    report.beliefs_held_after = (
        total_held - report.merges - report.archived
        if dry_run
        else len(list_beliefs(conn, currently_held=True, limit=100000, field_id=field_id))
    )
    return report


def render_report_markdown(report: BeliefConsolidationReport, sample: int = 25) -> str:
    """Render the consolidation report for docs/false-merge review."""
    lines = [
        "# Belief Consolidation — Report",
        "",
        f"_Generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}"
        f"{' (dry run)' if report.dry_run else ''}._",
        "",
        "## Summary",
        "",
        f"- Held beliefs before: **{report.beliefs_held_before}**",
        f"- Held beliefs after: **{report.beliefs_held_after}**",
        f"- Merges (duplicates absorbed): **{report.merges}**",
        f"- Auto-merges (high band): **{report.auto_merges}**",
        f"- LLM adjudications (middle band): **{report.adjudications}**",
        f"- Decayed: **{report.decayed}**",
        f"- Archived: **{report.archived}**",
        f"- Embeddings backfilled this run: **{report.embedded_now}**",
        "",
        f"## Sample of merges (up to {sample}, for false-merge review)",
        "",
    ]
    if not report.merge_records:
        lines.append("_No merges performed._")
    else:
        for rec in report.merge_records[:sample]:
            absorbed = ", ".join(
                f"{topic} (`{bid[:8]}`)" for bid, topic in rec.absorbed
            )
            lines.append(
                f"- **{rec.canonical_topic}** (`{rec.canonical_id[:8]}`) ← {absorbed}"
            )
    lines.append("")
    return "\n".join(lines)

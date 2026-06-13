"""One-time reconciliation of accumulated duplicate entities (Phase 13c).

Sweeps the whole entity table with the same block → match → merge logic the
live path uses, collapsing existing duplicates onto canonical nodes. Middle-band
adjudications are routed through the Anthropic Batch API (50% cheaper) when the
client supports it, falling back to synchronous calls otherwise.

Idempotent: merged duplicates are deleted, so a second run finds little to do.
``dry_run=True`` computes and reports the planned merges without writing.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from mesh_db.claims import count_claims, list_claims
from mesh_db.connection import MeshConnection
from mesh_db.entities import (
    choose_canonical,
    find_candidate_duplicates,
    get_entity_by_id,
    merge_entities,
    set_entity_embedding,
)
from mesh_llm import Embedder, entity_embed_text
from mesh_models.field import DEFAULT_FIELD_ID

from mesh_agents.entity_resolution import (
    EntityForMatch,
    EntityMatchDecision,
    ResolutionConfig,
    adjudicate_same_entity,
    build_adjudication_batch_items,
    classify_pair,
)


class _BatchLLM(Protocol):
    def submit_batch(self, items: list[Any], response_model: type) -> str: ...
    def batch_status(self, batch_id: str) -> str: ...
    def collect_batch(self, batch_id: str, response_model: type) -> dict[str, Any]: ...


@dataclass
class MergeRecord:
    canonical_id: str
    canonical_name: str
    absorbed: list[tuple[str, str]]  # (id, name)


@dataclass
class ReconciliationReport:
    entities_before: int = 0
    entities_after: int = 0
    embedded_now: int = 0
    auto_merges: int = 0          # high-band pairs
    adjudications: int = 0        # middle-band pairs sent to the LLM
    merges: int = 0               # entities absorbed (duplicates removed)
    dry_run: bool = False
    merge_records: list[MergeRecord] = field(default_factory=list)


@dataclass
class _Ent:
    id: str
    name: str
    type: str
    aliases: list[str]


def _load_entities(
    conn: MeshConnection, field_id: str = DEFAULT_FIELD_ID
) -> list[_Ent]:
    rows = conn.execute(
        "SELECT id, canonical_name, type, aliases FROM entities "
        "WHERE field_id = %s ORDER BY created_at",
        [field_id],
    ).fetchall()
    return [_Ent(str(r[0]), str(r[1]), str(r[2]), list(r[3] or [])) for r in rows]


def _ensure_embeddings(
    conn: MeshConnection,
    embedder: Embedder,
    ents: list[_Ent],
    field_id: str = DEFAULT_FIELD_ID,
) -> tuple[dict[str, list[float]], int]:
    """Embed every entity name in memory (used as query vectors) and persist any
    that the DB is missing. Returns ``(id→vector, count_persisted)``."""
    missing = {
        str(r[0])
        for r in conn.execute(
            "SELECT id FROM entities WHERE name_embedding IS NULL AND field_id = %s",
            [field_id],
        ).fetchall()
    }
    vectors = embedder.embed([entity_embed_text(e.name, e.type) for e in ents])
    by_id = {e.id: v for e, v in zip(ents, vectors, strict=True)}
    persisted = 0
    for e in ents:
        if e.id in missing:
            set_entity_embedding(conn, e.id, by_id[e.id])
            persisted += 1
    return by_id, persisted


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


def _entity_for_match(conn: MeshConnection, e: _Ent) -> EntityForMatch:
    claims = list_claims(conn, entity_id=e.id, limit=3)
    samples = [f"{c.predicate} {c.object}".strip() for c in claims]
    return EntityForMatch(
        canonical_name=e.name,
        entity_type=e.type,
        aliases=tuple(e.aliases),
        sample_claims=tuple(samples),
    )


def _pick_cluster_canonical(conn: MeshConnection, member_ids: list[str]) -> str:
    """Generalize choose_canonical to a cluster: most-claimed, tie-break earliest
    created_at, then smallest id."""
    best: tuple[int, Any, str] | None = None
    best_id = member_ids[0]
    for mid in member_ids:
        ent = get_entity_by_id(conn, mid)
        if ent is None:
            continue
        key = (-count_claims(conn, entity_id=mid), ent.created_at, mid)
        if best is None or key < best:
            best = key
            best_id = mid
    return best_id


def reconcile_entities(
    conn: MeshConnection,
    embedder: Embedder,
    llm: Any | None = None,
    *,
    config: ResolutionConfig | None = None,
    k: int = 10,
    dry_run: bool = False,
    batch_poll_seconds: float = 10.0,
    field_id: str = DEFAULT_FIELD_ID,
) -> ReconciliationReport:
    cfg = config or ResolutionConfig.from_env()
    report = ReconciliationReport(dry_run=dry_run)

    ents = _load_entities(conn, field_id)
    report.entities_before = len(ents)
    if not ents:
        return report

    vecs, report.embedded_now = _ensure_embeddings(conn, embedder, ents, field_id)
    by_id = {e.id: e for e in ents}

    # Block + classify → confirmed-same pairs and middle-band pairs to adjudicate.
    confirmed: set[frozenset[str]] = set()
    middle: set[frozenset[str]] = set()
    for e in ents:
        for cand_id, _name, _type, distance in find_candidate_duplicates(
            conn, vecs[e.id], entity_type=e.type, exclude_id=e.id, k=k, field_id=field_id
        ):
            pair = frozenset({e.id, cand_id})
            if len(pair) < 2:
                continue
            decision = classify_pair(1.0 - distance, cfg)
            if decision == "merge":
                confirmed.add(pair)
            elif decision == "adjudicate" and pair not in confirmed:
                middle.add(pair)

    report.auto_merges = len(confirmed)
    report.adjudications = len(middle)

    # Adjudicate the middle band (batch when supported, else synchronous).
    if middle:
        confirmed |= _adjudicate(conn, llm, by_id, middle, batch_poll_seconds)

    # Cluster confirmed pairs and merge each cluster onto one canonical.
    uf = _UnionFind()
    for pair in confirmed:
        a, b = sorted(pair)
        uf.union(a, b)

    for member_ids in uf.clusters().values():
        canonical = _pick_cluster_canonical(conn, member_ids)
        absorbed_ids = [m for m in member_ids if m != canonical]
        canonical_ent = get_entity_by_id(conn, canonical)
        canonical_name = canonical_ent.canonical_name if canonical_ent else canonical
        report.merge_records.append(
            MergeRecord(
                canonical_id=canonical,
                canonical_name=canonical_name,
                absorbed=[(m, by_id[m].name if m in by_id else m) for m in absorbed_ids],
            )
        )
        report.merges += len(absorbed_ids)
        if not dry_run:
            for dup in absorbed_ids:
                # Re-confirm direction defensively (claim counts may have shifted
                # as earlier merges in the cluster accrued onto the canonical).
                c, d = choose_canonical(conn, canonical, dup)
                merge_entities(conn, c, d)

    report.entities_after = (
        report.entities_before - report.merges
        if dry_run
        else len(_load_entities(conn, field_id))
    )
    return report


def _adjudicate(
    conn: MeshConnection,
    llm: Any | None,
    by_id: dict[str, _Ent],
    middle: set[frozenset[str]],
    batch_poll_seconds: float,
) -> set[frozenset[str]]:
    """Resolve middle-band pairs to confirmed-same via the LLM. Pairs whose
    endpoints are missing, or all pairs when ``llm`` is None, default to not-same
    (conservative)."""
    confirmed: set[frozenset[str]] = set()
    if llm is None:
        return confirmed

    pairs: list[tuple[str, str, str]] = []  # (custom_id, idA, idB)
    for pair in middle:
        a, b = sorted(pair)
        if a in by_id and b in by_id:
            pairs.append((f"{a}|{b}", a, b))
    if not pairs:
        return confirmed

    match_pairs = [
        (cid, _entity_for_match(conn, by_id[a]), _entity_for_match(conn, by_id[b]))
        for cid, a, b in pairs
    ]

    if hasattr(llm, "submit_batch"):
        batch_llm: _BatchLLM = llm
        items = build_adjudication_batch_items(match_pairs)
        batch_id = batch_llm.submit_batch(items, EntityMatchDecision)
        while batch_llm.batch_status(batch_id) != "ended":
            time.sleep(batch_poll_seconds)
        results = batch_llm.collect_batch(batch_id, EntityMatchDecision)
        for cid, a, b in pairs:
            parsed = getattr(results.get(cid), "parsed", None)
            if isinstance(parsed, EntityMatchDecision) and parsed.same_entity:
                confirmed.add(frozenset({a, b}))
    else:
        for _cid, a, b in pairs:
            decision = adjudicate_same_entity(
                llm, _entity_for_match(conn, by_id[a]), _entity_for_match(conn, by_id[b])
            )
            if decision.same_entity:
                confirmed.add(frozenset({a, b}))
    return confirmed


def render_report_markdown(report: ReconciliationReport, sample: int = 25) -> str:
    """Render the reconciliation report for docs/entity-resolution-reconciliation.md."""
    from datetime import UTC, datetime

    lines = [
        "# Entity Resolution — Reconciliation Report",
        "",
        f"_Generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}"
        f"{' (dry run)' if report.dry_run else ''}._",
        "",
        "## Summary",
        "",
        f"- Entities before: **{report.entities_before}**",
        f"- Entities after: **{report.entities_after}**",
        f"- Merges (duplicates absorbed): **{report.merges}**",
        f"- Auto-merges (high band): **{report.auto_merges}**",
        f"- LLM adjudications (middle band): **{report.adjudications}**",
        f"- Embeddings persisted this run: **{report.embedded_now}**",
        "",
        f"## Sample of merges (up to {sample}, for false-merge review)",
        "",
    ]
    if not report.merge_records:
        lines.append("_No merges performed._")
    else:
        for rec in report.merge_records[:sample]:
            absorbed = ", ".join(f"{name} (`{eid[:8]}`)" for eid, name in rec.absorbed)
            lines.append(
                f"- **{rec.canonical_name}** (`{rec.canonical_id[:8]}`) ← {absorbed}"
            )
    lines.append("")
    return "\n".join(lines)

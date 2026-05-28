"""Phase 7 pre-work — reproduction signal shape exploration.

One-off exploratory script. Not production code. Goal: understand the
shape of cross-source corroboration in the current DB before we design
the 7b reproduction-tracker view.

What we want to know:
- How many distinct source types corroborate the average (subject, predicate,
  object) triple?
- What's the distribution? Is it long-tailed (most triples appear once)?
- Are there examples of high-corroboration triples (likely SOTA results
  reproduced across arxiv + leaderboard + blog)?
- How fuzzy is "the same object"? Are benchmark scores reported with
  identical numeric values across sources, or do we need tolerance?

Run with::

    uv run python scripts/explore_reproduction.py

Writes to stdout. Findings → docs/reproduction-signal-exploration.md.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass
from typing import Any

from mesh_db.connection import get_connection


@dataclass
class TripleStat:
    subject_entity_id: str
    predicate: str
    object_key: str
    source_types: list[str]
    claim_count: int


def _object_key(obj_raw: Any) -> str:
    """A coarse 'same object' key for cross-source corroboration matching.

    Exact match on serialized JSON is too brittle — Skeptic counter-claims
    in particular often paraphrase. Pick a pragmatic middle:

    - achieves_score / outperforms / evaluated_on: round numeric scores to
      one decimal place, normalize benchmark names to lowercase. This is
      tight enough that "78.4 on MMLU" and "78.2 on MMLU" cluster together
      while "78.4 on MMLU" and "61.0 on HellaSwag" do not.
    - developed_by: just the canonical_name string.
    - everything else: full JSON.

    Refine after the exploration shows where this is too coarse or fine.
    """
    if isinstance(obj_raw, str):
        try:
            obj = json.loads(obj_raw)
        except json.JSONDecodeError:
            return obj_raw
    else:
        obj = obj_raw or {}
    if not isinstance(obj, dict):
        return json.dumps(obj, sort_keys=True, default=str)
    benchmark = obj.get("benchmark")
    score = obj.get("score")
    if benchmark is not None and score is not None:
        try:
            return f"benchmark={str(benchmark).lower()}|score={float(score):.1f}"
        except (TypeError, ValueError):
            return f"benchmark={benchmark}|score={score}"
    org = obj.get("organization") or obj.get("developer")
    if org is not None:
        return f"org={str(org).lower()}"
    return json.dumps(obj, sort_keys=True, default=str)


def main() -> None:
    db_path = os.environ.get("MESH_DB_PATH", "./data/mesh.db")
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT c.subject_entity_id, c.predicate, c.object, s.type, COUNT(*)
            FROM claims c
            JOIN sources s ON s.id = c.source_id
            GROUP BY c.subject_entity_id, c.predicate, c.object, s.type
            """
        ).fetchall()
    finally:
        conn.close()

    triples: dict[tuple[str, str, str], dict[str, int]] = {}
    for subj, pred, obj_raw, src_type, cnt in rows:
        key = (subj, pred, _object_key(obj_raw))
        triples.setdefault(key, {})[src_type] = (
            triples.get(key, {}).get(src_type, 0) + int(cnt)
        )

    if not triples:
        print("No claims in DB — nothing to analyze.")
        print("Re-run `make pipeline` to populate before the next exploration.")
        return

    stats = [
        TripleStat(
            subject_entity_id=key[0],
            predicate=key[1],
            object_key=key[2],
            source_types=sorted(types.keys()),
            claim_count=sum(types.values()),
        )
        for key, types in triples.items()
    ]
    print(f"Total distinct (subject, predicate, object_key) triples: {len(stats)}")
    print()

    # Distribution of distinct-source-types per triple
    dist = Counter(len(s.source_types) for s in stats)
    print("Source-type breadth per triple:")
    for n_types in sorted(dist):
        pct = 100.0 * dist[n_types] / len(stats)
        print(f"  {n_types} type(s): {dist[n_types]} triples ({pct:.1f}%)")
    print()

    # High corroboration examples
    high = sorted(stats, key=lambda s: -len(s.source_types))[:5]
    print("Top 5 by source-type breadth:")
    for s in high:
        print(
            f"  predicate={s.predicate} subject={s.subject_entity_id[:8]}… "
            f"types={s.source_types} object_key={s.object_key}"
        )
    print()

    # Single-source examples
    singletons = [s for s in stats if len(s.source_types) == 1][:5]
    if singletons:
        print("First 5 single-source triples (likely uncorroborated):")
        for s in singletons:
            print(
                f"  predicate={s.predicate} subject={s.subject_entity_id[:8]}… "
                f"only={s.source_types[0]} object_key={s.object_key}"
            )
    print()

    # Predicate-level breakdown — useful to know which predicates corroborate
    # most across source types, which inform 7b weighting
    pred_breadth: dict[str, list[int]] = {}
    for s in stats:
        pred_breadth.setdefault(s.predicate, []).append(len(s.source_types))
    print("Per-predicate average source-type breadth:")
    for pred, breadths in sorted(pred_breadth.items()):
        avg = sum(breadths) / len(breadths)
        print(f"  {pred}: avg {avg:.2f} types over {len(breadths)} triples")


if __name__ == "__main__":
    main()

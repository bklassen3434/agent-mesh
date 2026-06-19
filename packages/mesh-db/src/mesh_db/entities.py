from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from mesh_models.entity import Entity, EntityType
from mesh_models.field import DEFAULT_FIELD_ID
from psycopg.types.json import Jsonb

from mesh_db.connection import MeshConnection


def _row_to_entity(row: tuple[Any, ...]) -> Entity:
    id_, canonical_name, aliases, type_, attributes, created_at, last_seen_at = row[:7]
    return Entity(
        id=id_,
        canonical_name=canonical_name,
        aliases=list(aliases) if aliases else [],
        type=EntityType(type_),
        attributes=json.loads(attributes) if isinstance(attributes, str) else (attributes or {}),
        created_at=(
            created_at if isinstance(created_at, datetime)
            else datetime.fromisoformat(str(created_at))
        ),
        last_seen_at=(
            last_seen_at if isinstance(last_seen_at, datetime)
            else datetime.fromisoformat(str(last_seen_at))
        ),
    )


def create_entity(
    conn: MeshConnection, model: Entity, *, field_id: str = DEFAULT_FIELD_ID
) -> Entity:
    conn.execute(
        """
        INSERT INTO entities
            (id, field_id, canonical_name, aliases, type, attributes,
             created_at, last_seen_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        [
            model.id,
            field_id,
            model.canonical_name,
            model.aliases,
            model.type.value,
            Jsonb(model.attributes),
            model.created_at,
            model.last_seen_at,
        ],
    )
    return model


def _vector_literal(embedding: list[float]) -> str:
    """pgvector text input format: ``[0.1,0.2,...]``. Used with a ``::vector``
    cast so we need no extra psycopg type adapter."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def set_entity_embedding(
    conn: MeshConnection, id: str, embedding: list[float]
) -> None:
    """Populate ``name_embedding`` for an entity (Phase 13). Mutating the
    embedding is a resolution-layer write, not a claim/identity content change."""
    conn.execute(
        "UPDATE entities SET name_embedding = %s::vector WHERE id = %s",
        [_vector_literal(embedding), id],
    )


def find_candidate_duplicates(
    conn: MeshConnection,
    embedding: list[float],
    *,
    entity_type: EntityType | str | None = None,
    k: int = 10,
    exclude_id: str | None = None,
    field_id: str = DEFAULT_FIELD_ID,
) -> list[tuple[str, str, str, float]]:
    """Blocking query: the ``k`` nearest existing entities by cosine distance.

    Returns ``(id, canonical_name, type, distance)`` ordered nearest-first.
    Cosine distance (``<=>``) is in ``[0, 2]``; similarity is ``1 - distance``
    for the normalised vectors the embedder produces. Always scoped to
    ``field_id`` (resolution never crosses fields — Phase 17a). Optionally
    filtered by entity type (a model never blocks against a benchmark) and
    excluding a given id (so an entity does not match itself)."""
    conditions = ["name_embedding IS NOT NULL", "field_id = %s"]
    params: list[Any] = [_vector_literal(embedding), field_id]
    if entity_type is not None:
        conditions.append("type = %s")
        params.append(
            entity_type.value if isinstance(entity_type, EntityType) else entity_type
        )
    if exclude_id is not None:
        conditions.append("id <> %s")
        params.append(exclude_id)
    params.append(max(int(k), 0))
    rows = conn.execute(
        f"""
        SELECT id, canonical_name, type, name_embedding <=> %s::vector AS distance
        FROM entities
        WHERE {' AND '.join(conditions)}
        ORDER BY distance
        LIMIT %s
        """,
        params,
    ).fetchall()
    return [(str(r[0]), str(r[1]), str(r[2]), float(r[3])) for r in rows]


def get_entity_by_id(conn: MeshConnection, id: str) -> Entity | None:
    row = conn.execute(
        "SELECT id, canonical_name, aliases, type, attributes, created_at, last_seen_at "
        "FROM entities WHERE id = %s",
        [id],
    ).fetchone()
    return _row_to_entity(row) if row else None


MAX_LIMIT = 200


def list_entities(
    conn: MeshConnection,
    type: EntityType | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
    field_id: str | None = None,
) -> list[Entity]:
    limit = min(max(limit, 0), MAX_LIMIT)
    offset = max(offset, 0)
    query = (
        "SELECT id, canonical_name, aliases, type, attributes, created_at, last_seen_at "
        "FROM entities"
    )
    conditions: list[str] = []
    params: list[Any] = []
    if field_id is not None:
        conditions.append("field_id = %s")
        params.append(field_id)
    if type is not None:
        conditions.append("type = %s")
        params.append(type.value)
    if q:
        conditions.append("canonical_name ILIKE %s")
        params.append(f"%{q}%")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    return [_row_to_entity(r) for r in rows]


def under_evidenced_entities(
    conn: MeshConnection,
    *,
    field_id: str = DEFAULT_FIELD_ID,
    max_claims: int = 1,
    limit: int = 50,
) -> list[tuple[Entity, int]]:
    """Entities the mesh barely knows anything about (Phase 22c gap signal).

    Returns ``(entity, claim_count)`` for entities in ``field_id`` whose number
    of claims (any claim whose ``subject_entity_id`` is the entity) is at most
    ``max_claims`` — i.e. zero/one supporting claim. Thinnest-first. A single
    aggregate (no N+1). Field-scoped: the entity filter pins the field, and a
    claim's subject is an entity in the same field by construction."""
    rows = conn.execute(
        """
        SELECT e.id, e.canonical_name, e.aliases, e.type, e.attributes,
               e.created_at, e.last_seen_at, COUNT(c.id) AS claim_count
        FROM entities e
        LEFT JOIN claims c ON c.subject_entity_id = e.id
        WHERE e.field_id = %s
        GROUP BY e.id, e.canonical_name, e.aliases, e.type, e.attributes,
                 e.created_at, e.last_seen_at
        HAVING COUNT(c.id) <= %s
        ORDER BY claim_count ASC, e.last_seen_at DESC
        LIMIT %s
        """,
        [field_id, max(int(max_claims), 0), max(int(limit), 0)],
    ).fetchall()
    return [(_row_to_entity(r), int(r[7])) for r in rows]


def count_entities(
    conn: MeshConnection,
    type: EntityType | None = None,
    q: str | None = None,
    field_id: str | None = None,
) -> int:
    query = "SELECT COUNT(*) FROM entities"
    conditions: list[str] = []
    params: list[Any] = []
    if field_id is not None:
        conditions.append("field_id = %s")
        params.append(field_id)
    if type is not None:
        conditions.append("type = %s")
        params.append(type.value)
    if q:
        conditions.append("canonical_name ILIKE %s")
        params.append(f"%{q}%")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    row = conn.execute(query, params).fetchone()
    return int(row[0]) if row else 0


def find_duplicate_candidate_pairs(
    conn: MeshConnection,
    *,
    field_id: str = DEFAULT_FIELD_ID,
    min_similarity: float = 0.80,
    limit: int = 50,
) -> list[tuple[str, str, str, str, float]]:
    """Pairs of same-type entities whose names embed close enough to be likely
    duplicates (agentic-migration tension: ``merge_candidate``). One pgvector
    self-join, field-scoped, each unordered pair once. Returns
    ``(id_a, name_a, id_b, name_b, similarity)`` most-similar first. Entities with
    no ``name_embedding`` are skipped."""
    distance_max = 1.0 - float(min_similarity)
    rows = conn.execute(
        """
        SELECT e1.id, e1.canonical_name, e2.id, e2.canonical_name,
               1 - (e1.name_embedding <=> e2.name_embedding) AS similarity
        FROM entities e1
        JOIN entities e2
          ON e2.field_id = e1.field_id
         AND e2.type = e1.type
         AND e2.id > e1.id
        WHERE e1.field_id = %s
          AND e1.name_embedding IS NOT NULL
          AND e2.name_embedding IS NOT NULL
          AND (e1.name_embedding <=> e2.name_embedding) <= %s
        ORDER BY similarity DESC
        LIMIT %s
        """,
        [field_id, distance_max, max(int(limit), 0)],
    ).fetchall()
    return [(str(r[0]), str(r[1]), str(r[2]), str(r[3]), float(r[4])) for r in rows]


def get_entities_by_ids(
    conn: MeshConnection, ids: list[str]
) -> list[Entity]:
    if not ids:
        return []
    placeholders = ",".join(["%s"] * len(ids))
    rows = conn.execute(
        "SELECT id, canonical_name, aliases, type, attributes, created_at, last_seen_at "
        f"FROM entities WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    return [_row_to_entity(r) for r in rows]


def update_entity(
    conn: MeshConnection, id: str, **fields: Any
) -> Entity:
    allowed = {"canonical_name", "aliases", "type", "attributes", "last_seen_at"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        entity = get_entity_by_id(conn, id)
        if entity is None:
            raise ValueError(f"Entity {id} not found")
        return entity

    set_clauses = []
    params: list[Any] = []
    for key, value in updates.items():
        set_clauses.append(f"{key} = %s")
        if key == "attributes":
            params.append(Jsonb(value))
        elif key == "type" and isinstance(value, EntityType):
            params.append(value.value)
        else:
            params.append(value)

    params.append(id)
    conn.execute(
        f"UPDATE entities SET {', '.join(set_clauses)} WHERE id = %s", params
    )
    entity = get_entity_by_id(conn, id)
    if entity is None:
        raise ValueError(f"Entity {id} not found after update")
    return entity


# ---------------------------------------------------------------------------
# Entity merge (Phase 13b) — consolidate a duplicate onto a canonical node.
# ---------------------------------------------------------------------------


def choose_canonical(conn: MeshConnection, id_a: str, id_b: str) -> tuple[str, str]:
    """Pick which of two entities is canonical. Returns ``(canonical, duplicate)``.

    Rule (documented, deterministic): the **most-claimed** entity wins (it
    carries the most provenance); ties break to the **earliest ``created_at``**,
    then to the **lexicographically smaller id**.
    """
    rows = conn.execute(
        "SELECT subject_entity_id, count(*) FROM claims "
        "WHERE subject_entity_id IN (%s, %s) GROUP BY subject_entity_id",
        [id_a, id_b],
    ).fetchall()
    counts = {str(r[0]): int(r[1]) for r in rows}
    ent_a = get_entity_by_id(conn, id_a)
    ent_b = get_entity_by_id(conn, id_b)
    if ent_a is None or ent_b is None:
        raise ValueError(f"Cannot choose canonical: {id_a} or {id_b} not found")

    # (claim_count desc, created_at asc, id asc) — first is canonical.
    def key(eid: str, ent: Entity) -> tuple[int, datetime, str]:
        return (-counts.get(eid, 0), ent.created_at, eid)

    ordered = sorted([(id_a, ent_a), (id_b, ent_b)], key=lambda t: key(t[0], t[1]))
    return ordered[0][0], ordered[1][0]


def _merge_aliases(canonical: Entity, duplicate: Entity) -> list[str]:
    """Union of canonical.aliases, duplicate.canonical_name, and
    duplicate.aliases; case-insensitive dedup, excluding the canonical's own
    name."""
    merged: list[str] = []
    seen: set[str] = {canonical.canonical_name.lower()}
    for alias in [
        *canonical.aliases,
        duplicate.canonical_name,
        *duplicate.aliases,
    ]:
        key = alias.lower()
        if key not in seen:
            seen.add(key)
            merged.append(alias)
    return merged


def _aggregate_duplicate_edges(conn: MeshConnection, canonical_id: str) -> None:
    """After re-pointing, collapse relationships that now collide on
    (from, to, type): keep the lowest-id row, union ``evidence_claim_ids``, take
    the max ``confidence``, delete the rest. Also drops canonical self-loops the
    merge created (an entity related to itself carries no signal here)."""
    conn.execute(
        "DELETE FROM relationships "
        "WHERE from_entity_id = %s AND to_entity_id = %s",
        [canonical_id, canonical_id],
    )
    rows = conn.execute(
        "SELECT id, from_entity_id, to_entity_id, type, evidence_claim_ids, confidence "
        "FROM relationships WHERE from_entity_id = %s OR to_entity_id = %s "
        "ORDER BY id",
        [canonical_id, canonical_id],
    ).fetchall()

    groups: dict[tuple[str, str, str], list[tuple[Any, ...]]] = {}
    for r in rows:
        groups.setdefault((str(r[1]), str(r[2]), str(r[3])), []).append(r)

    for members in groups.values():
        if len(members) < 2:
            continue
        keeper = members[0]
        evidence: list[str] = []
        seen: set[str] = set()
        max_conf = 0.0
        for m in members:
            for cid in (m[4] or []):
                if cid not in seen:
                    seen.add(cid)
                    evidence.append(str(cid))
            max_conf = max(max_conf, float(m[5]))
        conn.execute(
            "UPDATE relationships SET evidence_claim_ids = %s, confidence = %s "
            "WHERE id = %s",
            [evidence, max_conf, keeper[0]],
        )
        dup_ids = [m[0] for m in members[1:]]
        placeholders = ",".join(["%s"] * len(dup_ids))
        conn.execute(
            f"DELETE FROM relationships WHERE id IN ({placeholders})", dup_ids
        )


def merge_entities(conn: MeshConnection, canonical_id: str, duplicate_id: str) -> None:
    """Consolidate ``duplicate_id`` into ``canonical_id`` in a single transaction.

    Re-points every reference to the duplicate — claim ``subject_entity_id``
    (the FK only; claim *content* is never touched), relationship edges,
    investigation ``target_entity_id`` / ``related_entity_ids`` — aggregates any
    edges that become duplicates, folds the duplicate's name + aliases into the
    canonical's ``aliases``, and finally deletes the duplicate row. Coordinator/
    writer-owned. Idempotent-friendly: a no-op if the duplicate no longer exists.
    """
    if canonical_id == duplicate_id:
        return
    canonical = get_entity_by_id(conn, canonical_id)
    duplicate = get_entity_by_id(conn, duplicate_id)
    if duplicate is None:
        return  # already merged / gone
    if canonical is None:
        raise ValueError(f"Canonical entity {canonical_id} not found")

    merged_aliases = _merge_aliases(canonical, duplicate)
    last_seen = max(canonical.last_seen_at, duplicate.last_seen_at)

    with conn.raw.transaction():
        # 1. Re-point claims (FK reference only — predicate/object/excerpt untouched).
        conn.execute(
            "UPDATE claims SET subject_entity_id = %s WHERE subject_entity_id = %s",
            [canonical_id, duplicate_id],
        )
        # 2. Re-point relationship endpoints.
        conn.execute(
            "UPDATE relationships SET from_entity_id = %s WHERE from_entity_id = %s",
            [canonical_id, duplicate_id],
        )
        conn.execute(
            "UPDATE relationships SET to_entity_id = %s WHERE to_entity_id = %s",
            [canonical_id, duplicate_id],
        )
        # 3. Re-point investigation references.
        conn.execute(
            "UPDATE investigations SET target_entity_id = %s WHERE target_entity_id = %s",
            [canonical_id, duplicate_id],
        )
        conn.execute(
            "UPDATE investigations "
            "SET related_entity_ids = array_replace(related_entity_ids, %s, %s) "
            "WHERE %s = ANY(related_entity_ids)",
            [duplicate_id, canonical_id, duplicate_id],
        )
        # 4. Aggregate edges that now collide; drop self-loops.
        _aggregate_duplicate_edges(conn, canonical_id)
        # 5. Fold aliases + refresh last_seen on the canonical.
        conn.execute(
            "UPDATE entities SET aliases = %s, last_seen_at = %s WHERE id = %s",
            [merged_aliases, last_seen, canonical_id],
        )
        # 6. Remove the now-unreferenced duplicate.
        conn.execute("DELETE FROM entities WHERE id = %s", [duplicate_id])

"""Agent-side field-profile loading (Phase 17b).

Agents build their system prompt from the active field's ``FieldProfile``. The
coordinator passes a ``field_id`` in each skill payload; the agent loads the
profile (best-effort, off a ``mesh_reader`` connection) and caches it per
field_id so the ``cache_control``-marked system prefix is byte-stable within —
and across — a field's runs. When no DB is reachable (DB-less tests / minimal
setups) or the field is unknown, it degrades to the seeded ai-robotics profile,
preserving prior behavior.
"""
from __future__ import annotations

import logging

from mesh_models.field import AI_ROBOTICS_PROFILE, DEFAULT_FIELD_ID, FieldProfile

logger = logging.getLogger(__name__)

# Process-local cache: field_id → profile. Profiles change rarely; a stable
# per-field prefix matters more than freshness, so we cache for the process.
_CACHE: dict[str, FieldProfile] = {DEFAULT_FIELD_ID: AI_ROBOTICS_PROFILE}


def load_profile(field_id: str = DEFAULT_FIELD_ID) -> FieldProfile:
    """Return the FieldProfile for ``field_id`` (cached). Falls back to the
    seeded ai-robotics profile when the DB is unreachable or the field is
    unknown — a profile read must never break a skill."""
    cached = _CACHE.get(field_id)
    if cached is not None:
        return cached
    try:
        from mesh_db.connection import get_connection
        from mesh_db.fields import get_field

        conn = get_connection(read_only=True)
        try:
            field = get_field(conn, field_id)
        finally:
            conn.close()
        profile = field.profile if field is not None else AI_ROBOTICS_PROFILE
    except Exception as exc:  # no DSN / unreachable / unknown — degrade
        logger.debug("profile_load_failed", extra={"field_id": field_id, "error": str(exc)})
        profile = AI_ROBOTICS_PROFILE
    _CACHE[field_id] = profile
    return profile


def clear_cache() -> None:
    """Reset the per-process profile cache (test helper)."""
    _CACHE.clear()
    _CACHE[DEFAULT_FIELD_ID] = AI_ROBOTICS_PROFILE

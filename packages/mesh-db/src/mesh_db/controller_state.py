"""Deterministic controller — per-tension dispatch state.

The rule-based controller (the auction-free orchestrator that replaced the
market) decides what to do as a pure function of three things: the board's
current tensions, these stored per-tension counters, and the ``now`` it is
handed. This module is the counters: one row per ``(field, tension)`` recording
how many times the controller has dispatched that tension and how the last
attempt went.

Why it exists: temporal and escalation rules need memory the board doesn't have.
"Don't re-scout for 10 minutes" is ``now - last_attempt_at >= 600`` against a
*stored* timestamp; "escalate to a swarm after 3 failed attempts" is
``attempts >= 3 AND last_outcome != 'effects'``. Both are pure arithmetic over
these rows + ``now`` — so the controller needs no daemon and no wall-clock
watcher; whoever invokes it (scheduler tick, post-run hook, CLI) gets the same
deterministic decision, and invoking it more often is harmless.

Operational state, not knowledge: the controller owns these writes directly
(writer role), like the ``pipeline_runs`` ledger — they never flow through the
effect gateway. Tension ids are stable (``"<kind>:<target>"``), so a row's
identity survives recomputation of the board.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from mesh_models.field import DEFAULT_FIELD_ID
from pydantic import BaseModel

from mesh_db.connection import MeshConnection


class DispatchOutcome(StrEnum):
    """How the last dispatch of a tension turned out."""

    effects = "effects"  # the skill produced at least one effect
    no_effects = "no_effects"  # ran but produced nothing (a stall signal)
    error = "error"  # the skill raised (caught; recorded)


class TensionState(BaseModel):
    """The stored counters for one tension (the controller's memory of it)."""

    field_id: str
    tension_id: str
    attempts: int = 0
    last_outcome: DispatchOutcome | None = None
    last_effect_count: int = 0
    last_attempt_at: datetime | None = None

    def seconds_since_attempt(self, now: datetime) -> float | None:
        """Wall-clock seconds since the last dispatch (None if never dispatched).
        The numeric input a cooldown rule compares against — ``now`` is passed in,
        ``last_attempt_at`` is stored, so the comparison is pure and daemon-free."""
        if self.last_attempt_at is None:
            return None
        return (now - self.last_attempt_at).total_seconds()

    @property
    def stalled(self) -> bool:
        """The last dispatch ran but changed nothing — the escalation trigger."""
        return self.last_outcome in (DispatchOutcome.no_effects, DispatchOutcome.error)


def get_tension_states(
    conn: MeshConnection, field_id: str = DEFAULT_FIELD_ID
) -> dict[str, TensionState]:
    """All stored tension counters for a field, keyed by ``tension_id``. One read
    per controller round — the rules consult this map, never the table directly."""
    rows = conn.execute(
        """
        SELECT field_id, tension_id, attempts, last_outcome, last_effect_count,
               last_attempt_at
        FROM controller_tension_state
        WHERE field_id = %s
        """,
        [field_id],
    ).fetchall()
    out: dict[str, TensionState] = {}
    for r in rows:
        out[str(r[1])] = TensionState(
            field_id=str(r[0]),
            tension_id=str(r[1]),
            attempts=int(r[2]),
            last_outcome=DispatchOutcome(r[3]) if r[3] is not None else None,
            last_effect_count=int(r[4]),
            last_attempt_at=_dt(r[5]) if r[5] is not None else None,
        )
    return out


def record_dispatch(
    conn: MeshConnection,
    field_id: str,
    tension_id: str,
    outcome: DispatchOutcome,
    effect_count: int,
    now: datetime | None = None,
) -> None:
    """Upsert a tension's counters after the controller dispatches it: bump
    ``attempts``, store the outcome / effect count, stamp ``last_attempt_at``.
    Called once per dispatched tension per round (writer-owned)."""
    ts = now or datetime.now(UTC)
    conn.execute(
        """
        INSERT INTO controller_tension_state
            (field_id, tension_id, attempts, last_outcome, last_effect_count,
             last_attempt_at)
        VALUES (%s, %s, 1, %s, %s, %s)
        ON CONFLICT (field_id, tension_id) DO UPDATE SET
            attempts = controller_tension_state.attempts + 1,
            last_outcome = excluded.last_outcome,
            last_effect_count = excluded.last_effect_count,
            last_attempt_at = excluded.last_attempt_at
        """,
        [field_id, tension_id, outcome.value, effect_count, ts],
    )


def _dt(val: object) -> datetime:
    return val if isinstance(val, datetime) else datetime.fromisoformat(str(val))

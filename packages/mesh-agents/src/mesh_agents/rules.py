"""The deterministic rule engine — what replaced the market's auction.

The old market let skills *bid* (value / cost) and funded the highest
value-per-dollar offers under a budget — selection was an emergent price. This
module is the replacement: an **explicit, ordered table of rules**. A rule is a
pure function of :class:`ControllerState` — the board's current tensions, the
stored per-tension counters (``mesh_db.controller_state``), and the ``now`` it is
handed — that yields zero or more :class:`Activation`s (a tension + the skill to
run + an integer priority + a fanout). No prices, no budget knapsack: the
controller dispatches activations in priority order under a step cap.

Three things make this fully deterministic and daemon-free, which is the whole
point of the redesign:

* **Routing is a 1:1 map.** Each tension names its handler skill
  (``Tension.handler_skill``); a rule just forwards it. There was never more than
  one skill per kind, so nothing needed an auction to choose.
* **Temporal conditions are state conditions.** "Scout when the field has been
  quiescent for 10 minutes" is not a wall-clock watcher — it is "the board has no
  actionable knowledge tension AND ``now - last_scout_at >= cooldown``", pure
  arithmetic over a *stored* timestamp and the passed-in ``now``. Whoever invokes
  the controller (scheduler, post-run hook, CLI) gets the same answer.
* **Escalation is a counter condition.** "A skill couldn't resolve it — spawn a
  swarm" is "this tension has been dispatched ``>= N`` times and the last attempt
  changed nothing" → re-route the same tension to its skill with ``fanout = K``
  (K parallel instances, effects unioned). Stalls are read from stored counters.

Adding a rule is appending one entry to :data:`RULES`. Priorities are explicit
integers (lower = more urgent); the planner sorts ``(priority, -salience, id)``
and lets the most-urgent activation win per tension, so an escalation
(priority 0) cleanly pre-empts the normal handler (priority 10+) for the same
tension.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from mesh_db.controller_state import TensionState
from mesh_models.tension import ReasoningTier, Tension, TensionKind
from pydantic import BaseModel


# ── config (all deterministic numbers; no timers) ────────────────────────────
def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def escalate_after() -> int:
    """Stalled-dispatch count past which a tension escalates to a swarm."""
    return _int_env("MESH_CONTROLLER_ESCALATE_AFTER", 3)


def swarm_size() -> int:
    """How many parallel skill instances an escalation fans out to."""
    return max(1, _int_env("MESH_CONTROLLER_SWARM_SIZE", 3))


def scout_cooldown_seconds() -> float:
    """Minimum seconds between scouts of the same connector once the board is
    otherwise idle (the deterministic form of "quiescent for 10 minutes")."""
    return _float_env("MESH_CONTROLLER_SCOUT_COOLDOWN_SEC", 600.0)


def scout_max_staleness_seconds() -> float:
    """Safety valve: the longest a connector may go un-scouted **even while the
    board is busy**. Normally scouting waits for an idle board (drain the backlog
    first), but a persistent backlog must never starve ingestion indefinitely — a
    stuck synthesize loop once kept the board "busy" for a week and no new sources
    were pulled. Past this age a connector scouts regardless of backlog. Clamped to
    at least the cooldown."""
    return max(
        _float_env("MESH_CONTROLLER_SCOUT_MAX_STALENESS_SEC", 86400.0),
        scout_cooldown_seconds(),
    )


def maintenance_cooldown_seconds() -> float:
    """Minimum seconds between periodic, LLM-free maintenance passes (belief
    aging, memory consolidation) — the deterministic form of "run this daily".
    Like the scout cooldown, it is pure arithmetic over a stored last-attempt
    timestamp, so the controller stays daemon-free."""
    return _float_env("MESH_CONTROLLER_MAINTAIN_COOLDOWN_SEC", 86_400.0)


def stall_cooldown_seconds() -> float:
    """Backoff before re-dispatching a tension whose last attempt produced nothing
    (``no_effects`` / ``error``).

    Some tensions are *board-derived* and persist until the underlying gap is
    actually filled — e.g. a ``rising_topic`` or ``thin_belief`` the
    ``investigate-gap`` skill can't act on because the investigation backlog is
    already full. Without a backoff these re-fire on **every** pass forever: the
    self-driving loop counts the no-effect dispatch as "work" so it never idles,
    and each dispatch writes an ``agent_invocations`` row — a real incident once
    accreted 1.6M no-effect rows in a day (2.5 GB, 94% of the DB). Deep tensions
    are exempt from swarm escalation, so this cooldown is their only brake.

    Pure arithmetic over the stored ``last_attempt_at`` vs the passed-in ``now`` —
    no daemon, same deterministic answer whoever invokes the controller."""
    return _float_env("MESH_CONTROLLER_STALL_COOLDOWN_SEC", 3600.0)


# Priority tiers (lower = more urgent). Explicit and total — the whole ordering
# of the system lives here, not in an emergent price.
P_ESCALATE = 0  # a stalled tension pre-empts its own normal handler
P_EXTRACT = 10  # read what we already have (cheap, foundational)
P_RESOLVE = 20  # de-duplicate entities before synthesising on top of them
P_ADJUDICATE = 22  # adjudicate a contradicted load-bearing belief before building on it
P_CONSOLIDATE = 25  # de-duplicate beliefs (the "very similar beliefs" rule)
P_SYNTHESIZE = 30  # turn claims into beliefs
P_DISPATCH_INV = 35  # gather evidence for open investigations
P_CHALLENGE = 40  # re-examine contested / stale beliefs
P_INVESTIGATE = 50  # open investigations for knowledge gaps
P_MAINTAIN = 80  # periodic LLM-free housekeeping (belief aging, memory) when due
P_SCOUT = 90  # acquire new material only when otherwise idle


class Activation(BaseModel):
    """A rule's decision to run one skill on one tension this round.

    ``priority`` orders dispatch (lower first); ``fanout`` is how many parallel
    instances of the skill to run (1 normally, K on escalation — a swarm).
    ``reason`` is a human-readable trace of which rule fired and why."""

    tension: Tension
    skill_id: str
    priority: int
    fanout: int = 1
    reason: str

    @property
    def salience(self) -> float:
        """Secondary sort key within a priority tier — the board's own value
        estimate, so more-valuable work floats up among equally-urgent items."""
        return self.tension.value


@dataclass(frozen=True)
class ControllerState:
    """Everything a rule may read — and nothing it may write. A pure snapshot:
    the board's tensions, the stored per-tension counters, and ``now``."""

    field_id: str
    field_slug: str
    tensions: list[Tension]
    states: dict[str, TensionState]
    now: datetime
    dispatched: set[str] = field(default_factory=set)

    def state_for(self, tension_id: str) -> TensionState | None:
        return self.states.get(tension_id)

    def tensions_of(self, *kinds: TensionKind) -> list[Tension]:
        kindset = set(kinds)
        return [t for t in self.tensions if t.kind in kindset]

    def has_actionable_knowledge_work(self) -> bool:
        """True if any non-scout tension is still waiting — the signal that the
        board is *not* idle, so scouting should hold off and let it drain first."""
        return any(
            t.kind is not TensionKind.unscouted_connector
            and t.id not in self.dispatched
            for t in self.tensions
        )


@dataclass(frozen=True)
class Rule:
    """One named, deterministic condition→action over the board state."""

    name: str
    evaluate: Callable[[ControllerState], list[Activation]]


# ── the rules ────────────────────────────────────────────────────────────────
# Board-derived rules: forward a tension kind to its 1:1 handler skill at a fixed
# priority. These are the bulk of the work and need no runtime state.
def _handler_rule(
    name: str, kinds: tuple[TensionKind, ...], priority: int, why: str
) -> Rule:
    def evaluate(state: ControllerState) -> list[Activation]:
        out: list[Activation] = []
        for t in state.tensions_of(*kinds):
            # A swarm-tier tension runs K parallel copies from the first dispatch
            # (one answer, noisy path — union/quorum them); simple and deep run a
            # single instance (deep gets its depth from the across-rounds loop, not
            # parallel clones).
            fanout = swarm_size() if t.tier is ReasoningTier.swarm else 1
            out.append(
                Activation(
                    tension=t,
                    skill_id=t.handler_skill,
                    priority=priority,
                    fanout=fanout,
                    reason=why,
                )
            )
        return out

    return Rule(name=name, evaluate=evaluate)


def _in_stall_cooldown(state: ControllerState, t: Tension) -> bool:
    """True if ``t``'s last dispatch stalled (``no_effects`` / ``error``) and the
    stall cooldown has not yet elapsed — so it should not be re-dispatched this
    pass. A never-attempted tension, one that last produced effects, or one whose
    cooldown has elapsed is *not* cooling (returns False). Mirrors the scout /
    maintenance cooldowns: pure arithmetic over the stored ``last_attempt_at``."""
    st = state.state_for(t.id)
    if st is None or not st.stalled:
        return False
    elapsed = st.seconds_since_attempt(state.now)
    return elapsed is not None and elapsed < stall_cooldown_seconds()


def _escalate_stalled(state: ControllerState) -> list[Activation]:
    """A tension a skill keeps failing to resolve → re-route it to the same skill
    with ``fanout = swarm_size`` (a deep/parallel attempt). Pre-empts the normal
    handler for that tension (priority 0). Purely a counter condition: the last
    dispatch stalled (no effects / error) and attempts crossed the threshold."""
    threshold = escalate_after()
    k = swarm_size()
    out: list[Activation] = []
    for t in state.tensions:
        if t.kind is TensionKind.unscouted_connector or t.kind in _MAINTENANCE_KINDS:
            continue  # cheap + idempotent housekeeping; never worth a swarm
        # Deep tensions progress across rounds via their own state machine (the
        # gather investigation widens or abandons on its own budget); cloning a
        # stateful skill K times would just race, not deepen.
        if t.tier is ReasoningTier.deep:
            continue
        # Back off a just-stalled tension: re-escalate at most once per stall
        # cooldown, not every pass (a permanently-stuck swarm would otherwise spin
        # the daemon and flood the ledger, same failure mode as the gap rule).
        if _in_stall_cooldown(state, t):
            continue
        st = state.state_for(t.id)
        if st is None or st.attempts < threshold or not st.stalled:
            continue
        out.append(
            Activation(
                tension=t,
                skill_id=t.handler_skill,
                priority=P_ESCALATE,
                fanout=k,
                reason=(
                    f"escalate: {st.attempts} attempts, last outcome "
                    f"'{st.last_outcome}' → spawn swarm of {k}"
                ),
            )
        )
    return out


def _scout_when_idle(state: ControllerState) -> list[Activation]:
    """Acquire new material when the per-connector scout cooldown has elapsed and
    either the board is idle **or** the connector is egregiously overdue.

    The deterministic form of "if the field is quiescent for 10 minutes, run the
    scouts": the idle test is a board-state query, the cooldown is
    ``now - last_attempt_at >= cooldown`` over a stored timestamp — no daemon.

    Normally scouting waits for an idle board so the existing backlog drains first.
    But a persistent backlog must never starve ingestion forever (a stuck
    synthesize loop once kept the board "busy" for days with no new sources), so a
    connector past ``scout_max_staleness_seconds`` scouts regardless of backlog."""
    idle = not state.has_actionable_knowledge_work()
    cooldown = scout_cooldown_seconds()
    max_staleness = scout_max_staleness_seconds()
    out: list[Activation] = []
    for t in state.tensions_of(TensionKind.unscouted_connector):
        st = state.state_for(t.id)
        elapsed = st.seconds_since_attempt(state.now) if st else None
        # A never-scouted connector follows the normal idle gate (no bootstrap
        # scouting while a backlog exists); only one previously scouted but now
        # egregiously stale forces through regardless of backlog.
        overdue = elapsed is not None and elapsed >= max_staleness
        if not (idle or overdue):
            continue  # board busy and this connector isn't stale enough to force
        if elapsed is not None and elapsed < cooldown:
            continue  # scouted this connector too recently
        out.append(
            Activation(
                tension=t,
                skill_id=t.handler_skill,
                priority=P_SCOUT,
                reason=(
                    ("board idle" if idle else "overdue (backlog present)")
                    + (
                        f", {int(elapsed)}s since last scout"
                        if elapsed is not None
                        else ", never scouted"
                    )
                ),
            )
        )
    return out


# The knowledge-gap kinds all routed to the single ``investigate-gap`` skill.
_GAP_KINDS: tuple[TensionKind, ...] = (
    TensionKind.under_evidenced_entity,
    TensionKind.thin_belief,
    TensionKind.rising_topic,
    TensionKind.missing_reciprocal_edge,
)


def _investigate_knowledge_gaps(state: ControllerState) -> list[Activation]:
    """Open an investigation for a knowledge-gap tension — but skip one whose last
    investigate attempt stalled and is still within the stall cooldown.

    Unlike extract/synthesize (whose tensions vanish once handled), gap tensions
    are re-derived from board analysis every pass and persist until the gap is
    actually filled. When ``investigate-gap`` can't act — most commonly because the
    open-investigation backlog is full — it returns no effects, the gap stays, and
    without this cooldown it re-fires forever (the daemon never idles; the
    invocation ledger balloons). Gap kinds are deep/simple, never swarm, so escalation
    doesn't cover them — this cooldown is their brake."""
    out: list[Activation] = []
    for t in state.tensions_of(*_GAP_KINDS):
        if _in_stall_cooldown(state, t):
            continue  # investigated recently, produced nothing — back off
        fanout = swarm_size() if t.tier is ReasoningTier.swarm else 1
        out.append(
            Activation(
                tension=t,
                skill_id=t.handler_skill,
                priority=P_INVESTIGATE,
                fanout=fanout,
                reason="knowledge gap — open an investigation",
            )
        )
    return out


# The periodic maintenance kinds: cooldown-gated, LLM-free housekeeping that the
# controller runs on a timer rather than in response to board state.
_MAINTENANCE_KINDS: tuple[TensionKind, ...] = (
    TensionKind.aging_belief,
    TensionKind.consolidatable_memory,
    TensionKind.stale_field_brief,
)


def _maintain_when_due(state: ControllerState) -> list[Activation]:
    """Fire a periodic maintenance tension only once its cooldown has elapsed —
    the deterministic form of "run this daily". Same trick as ``_scout_when_idle``
    (temporal condition = ``now - last_attempt_at >= cooldown`` over a stored
    timestamp), but maintenance does not wait for the board to be idle: aging and
    memory upkeep are cheap and independent of the knowledge backlog."""
    cooldown = maintenance_cooldown_seconds()
    out: list[Activation] = []
    for t in state.tensions_of(*_MAINTENANCE_KINDS):
        st = state.state_for(t.id)
        elapsed = st.seconds_since_attempt(state.now) if st else None
        if elapsed is not None and elapsed < cooldown:
            continue  # handled this maintenance kind too recently
        out.append(
            Activation(
                tension=t,
                skill_id=t.handler_skill,
                priority=P_MAINTAIN,
                reason=(
                    "maintenance due"
                    + (
                        f", {int(elapsed)}s since last pass"
                        if elapsed is not None
                        else ", never run"
                    )
                ),
            )
        )
    return out


# The ordered rule table. Order is documentation; the planner sorts by the
# explicit priorities, so listing order does not affect dispatch — but reading
# top-to-bottom is the system's behaviour in one place.
RULES: tuple[Rule, ...] = (
    Rule(name="escalate-stalled", evaluate=_escalate_stalled),
    _handler_rule(
        "extract-unread",
        (TensionKind.unextracted_source,),
        P_EXTRACT,
        "unread source — extract its claims",
    ),
    _handler_rule(
        "resolve-duplicate-entities",
        (TensionKind.merge_candidate,),
        P_RESOLVE,
        "entities look like duplicates — adjudicate a merge",
    ),
    _handler_rule(
        "adjudicate-contradicted-beliefs",
        (TensionKind.contradicted_belief,),
        P_ADJUDICATE,
        "load-bearing belief contradicted by fresh evidence — gather + adjudicate",
    ),
    _handler_rule(
        "consolidate-redundant-beliefs",
        (TensionKind.redundant_beliefs,),
        P_CONSOLIDATE,
        "held beliefs look redundant — consolidate them",
    ),
    _handler_rule(
        "synthesize-claims",
        (TensionKind.unsynthesized_claims,),
        P_SYNTHESIZE,
        "claims not yet reflected in a belief — synthesise",
    ),
    _handler_rule(
        "dispatch-open-investigations",
        (TensionKind.open_investigation,),
        P_DISPATCH_INV,
        "open investigation — gather its evidence",
    ),
    _handler_rule(
        "challenge-contested-beliefs",
        (TensionKind.contested_claim, TensionKind.stale_belief),
        P_CHALLENGE,
        "belief is contested or stale — re-examine it",
    ),
    Rule(name="investigate-knowledge-gaps", evaluate=_investigate_knowledge_gaps),
    Rule(name="maintain-when-due", evaluate=_maintain_when_due),
    Rule(name="scout-when-idle", evaluate=_scout_when_idle),
)


def plan(state: ControllerState, rules: tuple[Rule, ...] = RULES) -> list[Activation]:
    """Evaluate every rule and return the deterministic, ordered worklist.

    Steps: run all rules → drop tensions already dispatched this run → keep the
    single most-urgent activation per tension (so an escalation pre-empts the
    normal handler) → sort by ``(priority, -salience, tension.id)``. The result
    is a total order: same board + same counters + same ``now`` → same plan."""
    best: dict[str, Activation] = {}
    for rule in rules:
        for act in rule.evaluate(state):
            if act.tension.id in state.dispatched:
                continue
            current = best.get(act.tension.id)
            if current is None or act.priority < current.priority:
                best[act.tension.id] = act
    return sorted(
        best.values(),
        key=lambda a: (a.priority, -a.salience, a.tension.id),
    )

"""The deterministic controller — the auction-free orchestrator.

This replaced the market loop. The market scanned the board, let skills *bid*
(value / cost), and funded the highest value-per-dollar offers under a budget.
The controller keeps the blackboard (the board → tensions sensing) but throws out
the auction: an explicit, ordered **rule table** (``mesh_agents.rules``) decides
what to dispatch and in what order, as a pure function of the board's tensions,
the stored per-tension counters (``mesh_db.controller_state``), and ``now``.

Per round it:

  1. **senses** the board into tensions (``compute_agenda`` + the operational
     scout / investigation producers) — the self-writing checklist;
  2. loads the stored per-tension counters and builds a read-only
     :class:`~mesh_agents.rules.ControllerState`;
  3. **plans** — every rule fires, the planner returns the deterministic ordered
     worklist (``mesh_agents.rules.plan``); a stalled tension's escalation
     pre-empts its normal handler (a swarm: ``fanout`` parallel instances);
  4. **dispatches** the top ``step_cap`` activations concurrently → effects;
  5. records each dispatch's outcome to the counters (so cooldowns / escalation
     stay deterministic across invocations) and applies effects via the gateway;
  6. repeats until the plan is empty (**quiescence**) or ``max_rounds``.

No budget, no prices, no daemon — the only knobs are a per-round ``step_cap`` and
``max_rounds``. ``shadow=True`` (the default) previews one round's plan + effects
and writes nothing (no effects, no counters); ``--apply`` acts and loops to
quiescence.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import click
import structlog
from mesh_agents.agenda import (
    compute_agenda,
    investigation_tensions,
    maintenance_tensions,
    scout_tensions,
)
from mesh_agents.confidence import BeliefSignals, ConfidenceWeights, compute_confidence
from mesh_agents.rules import Activation, ControllerState, plan
from mesh_agents.skill import all_skills, get_skill, load_builtin_skills
from mesh_db.beliefs import get_belief_signals
from mesh_db.connection import MeshConnection, get_connection
from mesh_db.controller_state import DispatchOutcome, get_tension_states, record_dispatch
from mesh_db.effects import ApplyReport, apply_effects
from mesh_db.fields import get_field_by_slug
from mesh_db.pg_migrations import init_pg
from mesh_db.pipeline_runs import PipelineRun, create_pipeline_run, pipeline_run_exists
from mesh_models.field import DEFAULT_FIELD_ID, DEFAULT_FIELD_SLUG
from mesh_models.tension import Tension
from pydantic import BaseModel, Field

log = structlog.get_logger()


def _make_confidence_fn() -> Any:
    """A confidence recompute the gateway applies after a belief's claim links are
    written — the evidence-derived score the coordinator's synthesize node uses
    (Phase 14d), so controller-synthesized / consolidated beliefs match
    coordinator quality instead of keeping the skill's prior."""
    weights = ConfidenceWeights.from_env()

    def confidence_fn(conn: MeshConnection, belief_id: str) -> float:
        return compute_confidence(
            BeliefSignals.from_row(get_belief_signals(conn, belief_id)), weights
        )

    return confidence_fn


def _get_concurrency() -> int:
    return int(os.environ.get("MESH_PIPELINE_CONCURRENCY", "3"))


def _get_step_cap() -> int:
    """Max activations dispatched per round (the deterministic replacement for the
    market's per-round budget). A large default — the loop runs to quiescence."""
    return int(os.environ.get("MESH_CONTROLLER_STEP_CAP", "8"))


def _swarm_quorum_enabled() -> bool:
    """Swarm reconcile mode. Off (default): union the K copies' effects (today's
    behaviour — good when the copies diverge and you want every distinct finding).
    On: keep an effect only if a *majority* of the instances that ran produced it
    — a quorum vote that suppresses a single copy's hallucination."""
    return os.environ.get("MESH_CONTROLLER_SWARM_QUORUM", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class RoundReport(BaseModel):
    """What one controller round saw, planned, and did."""

    round: int
    candidates: int  # tensions on the board this round
    planned: int  # activations the rules produced (after dedup)
    dispatched: int  # activations actually run (planned, capped, with a skill)
    skipped_no_skill: int  # activations whose skill isn't registered
    effects: int  # effects the dispatched skills emitted
    apply: ApplyReport | None = None  # None in shadow mode (nothing written)


class ControllerResult(BaseModel):
    run_id: str
    field_slug: str
    shadow: bool
    step_cap: int
    rounds: list[RoundReport] = Field(default_factory=list)
    quiescent: bool = False  # stopped because no rule fired (nothing worth doing)


def _sense(conn: Any, field_id: str, field_slug: str) -> list[Tension]:
    """Read the board into the full candidate tension list: the knowledge agenda
    plus the operational source-acquisition + investigation producers. Read-only;
    the same sensing the market used, minus the budget (the rules rank, not a
    price)."""
    agenda = compute_agenda(conn, field_id, field_slug=field_slug)
    return (
        scout_tensions(conn, field_id)
        + investigation_tensions(conn, field_id)
        + maintenance_tensions(conn, field_id)
        + agenda.tensions
    )


async def run_controller(
    field: str = DEFAULT_FIELD_SLUG,
    *,
    shadow: bool = True,
    max_rounds: int = 25,
    step_cap: int | None = None,
    now: datetime | None = None,
    conn: Any | None = None,
) -> ControllerResult:
    """Run the controller against one field until quiescence or ``max_rounds``.

    ``shadow`` (default) writes nothing — no effects, no counters — and previews a
    single round's plan. Set ``shadow=False`` to act and loop. ``now`` is injected
    for deterministic temporal rules (defaults to wall-clock UTC)."""
    load_builtin_skills()  # populate the registry
    log.info("controller_starting", field=field, shadow=shadow, skills=len(all_skills()))

    owns_conn = conn is None
    if conn is None:
        conn = get_connection()
        init_pg()
    field_row = get_field_by_slug(conn, field)
    field_id = field_row.id if field_row is not None else DEFAULT_FIELD_ID
    cap = step_cap if step_cap is not None else _get_step_cap()
    clock = now or datetime.now(UTC)

    result = ControllerResult(
        run_id=os.environ.get("MESH_RUN_ID") or str(uuid.uuid4()),
        field_slug=field,
        shadow=shadow,
        step_cap=cap,
    )
    semaphore = asyncio.Semaphore(_get_concurrency())
    confidence_fn = _make_confidence_fn()
    # Once-per-run guard: a tension dispatched this run is not re-planned in a
    # later round of the same run (tension ids are stable), so the loop reaches
    # quiescence. Cross-run escalation/cooldown lives in the persistent counters.
    dispatched: set[str] = set()

    try:
        for round_no in range(1, max_rounds + 1):
            # 1-2. sense the board + load stored counters → read-only state.
            tensions = _sense(conn, field_id, field)
            states = get_tension_states(conn, field_id)
            state = ControllerState(
                field_id=field_id,
                field_slug=field,
                tensions=tensions,
                states=states,
                now=clock,
                dispatched=dispatched,
            )

            # 3. plan: every rule fires → deterministic ordered worklist.
            activations = plan(state)
            if not activations:
                result.quiescent = True
                break
            selected = activations[:cap]

            # 4-5. dispatch (bounded), record outcomes, apply effects.
            effects, dispatched_count, skipped = await _dispatch_round(
                conn, selected, semaphore, shadow, clock, field_id, dispatched
            )
            apply_report: ApplyReport | None = None
            if not shadow and effects:
                apply_report = apply_effects(conn, effects, confidence_fn=confidence_fn)

            result.rounds.append(
                RoundReport(
                    round=round_no,
                    candidates=len(tensions),
                    planned=len(activations),
                    dispatched=dispatched_count,
                    skipped_no_skill=skipped,
                    effects=len(effects),
                    apply=apply_report,
                )
            )
            log.info(
                "controller_round",
                round=round_no,
                candidates=len(tensions),
                planned=len(activations),
                dispatched=dispatched_count,
                effects=len(effects),
                skipped_no_skill=skipped,
            )

            # 6. stop conditions: shadow previews one round; live stops when a
            # round dispatched nothing real (the board can no longer change).
            if shadow:
                break
            if dispatched_count == 0:
                result.quiescent = True
                break

        if not shadow:
            _record_run(conn, result, field_id)
    finally:
        if owns_conn:
            conn.close()

    log.info(
        "controller_complete",
        field=field,
        rounds=len(result.rounds),
        quiescent=result.quiescent,
    )
    return result


async def _dispatch_round(
    conn: Any,
    selected: list[Activation],
    semaphore: asyncio.Semaphore,
    shadow: bool,
    now: datetime,
    field_id: str,
    dispatched: set[str],
) -> tuple[list[Any], int, int]:
    """Dispatch the selected activations concurrently. Returns
    ``(effects, dispatched_count, skipped_no_skill)``. Records each dispatch's
    outcome to the persistent counters (live only) so escalation/cooldown stay
    deterministic. An activation whose skill isn't registered is counted as
    skipped and never marked dispatched."""
    runnable = [a for a in selected if get_skill(a.skill_id) is not None]
    skipped = len(selected) - len(runnable)

    results = await asyncio.gather(
        *(_dispatch_one(conn, a, semaphore) for a in runnable)
    )

    effects: list[Any] = []
    dispatched_count = 0
    for act, (act_effects, outcome) in zip(runnable, results, strict=True):
        effects.extend(act_effects)
        dispatched_count += 1
        dispatched.add(act.tension.id)
        # One structured line per dispatch — surfaces the reasoning tier and fanout
        # (simple / swarm-of-K / deep) so logs + Langfuse show how much reasoning
        # each tension drew, not just that a skill ran.
        log.info(
            "controller_dispatch",
            skill_id=act.skill_id,
            tension=act.tension.id,
            kind=act.tension.kind.value,
            tier=act.tension.tier.value,
            fanout=act.fanout,
            outcome=outcome.value,
            effects=len(act_effects),
        )
        if not shadow:
            record_dispatch(
                conn, field_id, act.tension.id, outcome, len(act_effects), now
            )
    return effects, dispatched_count, skipped


async def _dispatch_one(
    conn: Any, act: Activation, semaphore: asyncio.Semaphore
) -> tuple[list[Any], DispatchOutcome]:
    """Run one activation's skill — ``fanout`` instances in parallel (a swarm on
    escalation) — and union their effects (deduped). Returns the effects and the
    dispatch outcome (effects / no_effects / error) used to update the counters.

    A skill that raises contributes no effects and never aborts the round (the
    coordinator's one-bad-item philosophy). The outcome is ``error`` only when
    *every* instance raised, ``no_effects`` when they ran but produced nothing."""
    skill = get_skill(act.skill_id)
    assert skill is not None  # filtered by caller

    async def run_once() -> list[Any] | None:
        async with semaphore:
            try:
                return await skill.run(
                    conn, act.tension, budget_usd=act.tension.est_cost_usd
                )
            except Exception as exc:
                log.warning(
                    "skill_run_failed",
                    skill_id=act.skill_id,
                    tension=act.tension.id,
                    error=str(exc),
                )
                return None

    batches = await asyncio.gather(*(run_once() for _ in range(act.fanout)))
    ran = [b for b in batches if b is not None]
    if not ran:
        return [], DispatchOutcome.error

    # Reconcile effects across the instances that ran. Count how many *distinct*
    # instances produced each effect (by content). Quorum-off → keep any (union,
    # threshold 1); quorum-on → keep only effects a majority of instances agree on
    # (ceil(n/2)), so one copy's hallucination can't slip through. fanout=1 is a
    # no-op either way (threshold collapses to 1).
    counts: dict[str, int] = {}
    first: dict[str, Any] = {}
    for batch in ran:
        for key in {
            (eff.model_dump_json() if hasattr(eff, "model_dump_json") else repr(eff))
            for eff in batch
        }:
            counts[key] = counts.get(key, 0) + 1
        for eff in batch:
            key = eff.model_dump_json() if hasattr(eff, "model_dump_json") else repr(eff)
            first.setdefault(key, eff)
    threshold = (len(ran) + 1) // 2 if _swarm_quorum_enabled() else 1
    effects: list[Any] = [eff for key, eff in first.items() if counts[key] >= threshold]
    outcome = DispatchOutcome.effects if effects else DispatchOutcome.no_effects
    return effects, outcome


def _record_run(conn: Any, result: ControllerResult, field_id: str) -> None:
    """Aggregate the run's applied effects into a ``pipeline_runs`` row (run_type
    "controller"). Idempotent (run-exists guard) and best-effort — a ledger write
    must never abort the run, mirroring the coordinator's finalize."""
    try:
        if pipeline_run_exists(conn, result.run_id):
            return
        reports = [r.apply for r in result.rounds if r.apply is not None]

        def total(attr: str) -> int:
            return sum(getattr(rep, attr) for rep in reports)

        create_pipeline_run(
            conn,
            PipelineRun(
                id=result.run_id,
                finished_at=datetime.now(UTC),
                run_type="controller",
                triggered_by=os.environ.get("MESH_TRIGGERED_BY", "manual"),
                papers_scouted=total("sources_created"),
                sources_inserted=total("sources_created"),
                claims_inserted=total("claims_created"),
                entities_created=total("entities_created"),
                beliefs_created=total("beliefs_created"),
                beliefs_revised=total("beliefs_revised"),
            ),
            field_id=field_id,
        )
    except Exception as exc:  # never let the ledger abort the run
        log.warning("controller_run_record_failed", run_id=result.run_id, error=str(exc))


@click.command()
@click.option("--field", default=DEFAULT_FIELD_SLUG, show_default=True)
@click.option(
    "--step-cap",
    "step_cap",
    default=None,
    type=int,
    help="Max activations dispatched per round (default $MESH_CONTROLLER_STEP_CAP=8).",
)
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    default=False,
    help="Let the controller ACT (write through the gateway + loop to quiescence). "
    "Default is shadow mode: preview one round's plan, write nothing.",
)
def main(field: str, step_cap: int | None, apply_: bool) -> None:
    """Console entry point: `uv run mesh-controller` (shadow) / `--apply` (live)."""
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ]
    )
    result = asyncio.run(
        run_controller(field, shadow=not apply_, step_cap=step_cap)
    )
    mode = "LIVE" if apply_ else "SHADOW"
    print(f"\nController {result.run_id} [{result.field_slug}] — {mode}")
    print(f"  Step cap:  {result.step_cap}   Quiescent: {result.quiescent}")
    print(f"  Rounds:    {len(result.rounds)}")
    for r in result.rounds:
        print(
            f"  round {r.round}: {r.candidates} candidates, {r.planned} planned, "
            f"{r.dispatched} dispatched, {r.skipped_no_skill} no-skill, "
            f"{r.effects} effects"
        )
    if not result.rounds:
        print("  (board quiescent — nothing to do)")

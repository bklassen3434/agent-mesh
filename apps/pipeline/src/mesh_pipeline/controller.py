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
import time
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
from mesh_db.agent_invocations import create_agent_invocation
from mesh_db.beliefs import get_belief_signals
from mesh_db.connection import MeshConnection, get_connection
from mesh_db.controller_state import DispatchOutcome, get_tension_states, record_dispatch
from mesh_db.effects import ApplyReport, apply_effects
from mesh_db.fields import get_field_by_slug
from mesh_db.llm_usage import LLMUsageRecord, create_llm_usage
from mesh_db.pg_migrations import init_pg
from mesh_db.pipeline_runs import PipelineRun, create_pipeline_run, pipeline_run_exists
from mesh_llm.usage_sink import UsageEvent, open_sink
from mesh_models.agent_invocation import AgentInvocation
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


def _get_max_rounds() -> int:
    """Max sense→plan→dispatch rounds before one run gives up reaching quiescence.
    A cold start with a large board (e.g. the first ingest of a field) can need
    many rounds to drain; raise this so a run finishes the work instead of
    stopping mid-drain and leaving the rest for the next wake-up."""
    return int(os.environ.get("MESH_CONTROLLER_MAX_ROUNDS", "25"))


def _get_idle_sleep() -> float:
    """Seconds the self-driving loop waits after a pass that did nothing, before
    re-sensing. The only timing in continuous mode beyond the rules' own
    cooldowns — kept short (the world changes: scout cooldowns expire, new
    sources arrive) but not a busy-wait. Sensing is read-only, so this is cheap."""
    return float(os.environ.get("MESH_CONTROLLER_IDLE_SLEEP_SEC", "60"))


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
    max_rounds: int | None = None,
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
    rounds_cap = max_rounds if max_rounds is not None else _get_max_rounds()
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
        for round_no in range(1, rounds_cap + 1):
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
                conn, selected, semaphore, shadow, clock, field_id, dispatched,
                result.run_id,
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

        # Only ledger a run that actually did something — the self-driving loop
        # calls this every idle tick, and empty passes would otherwise flood
        # pipeline_runs with all-zero rows.
        if not shadow and any(r.dispatched for r in result.rounds):
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


async def run_controller_forever(
    field: str = DEFAULT_FIELD_SLUG,
    *,
    step_cap: int | None = None,
    idle_sleep_sec: float | None = None,
) -> None:
    """Self-driving mode: the controller IS the orchestrator — no external
    scheduler or cron.

    It holds one connection and repeats the full deterministic pass
    (sense → plan via the rule engine → dispatch agents → apply effects, looping
    to quiescence). When a pass does real work it immediately loops again (more
    may now be ready); when a pass finds nothing to do it idles briefly, then
    re-senses. All cadence comes from the rules themselves — scout cooldowns,
    maintenance cooldowns, new sources arriving — plus a short idle backoff
    between empty passes. Runs until the process is stopped."""
    idle = idle_sleep_sec if idle_sleep_sec is not None else _get_idle_sleep()
    conn = get_connection()
    init_pg()  # idempotent; done once for the process, not per pass
    log.info("controller_forever_starting", field=field, idle_sleep_sec=idle)
    try:
        while True:
            try:
                result = await run_controller(
                    field, shadow=False, step_cap=step_cap, conn=conn
                )
                did_work = any(r.dispatched for r in result.rounds)
            except Exception as exc:  # a bad pass must never kill the daemon
                log.warning("controller_pass_failed", field=field, error=str(exc))
                did_work = False
            if not did_work:
                await asyncio.sleep(idle)
    finally:
        conn.close()


async def _dispatch_round(
    conn: Any,
    selected: list[Activation],
    semaphore: asyncio.Semaphore,
    shadow: bool,
    now: datetime,
    field_id: str,
    dispatched: set[str],
    run_id: str,
) -> tuple[list[Any], int, int]:
    """Dispatch the selected activations concurrently. Returns
    ``(effects, dispatched_count, skipped_no_skill)``. Records each dispatch's
    outcome to the persistent counters AND its observability rows (live only) so
    escalation/cooldown stay deterministic and the agent/cost ledgers stay
    populated. An activation whose skill isn't registered is counted as skipped
    and never marked dispatched."""
    runnable = [a for a in selected if get_skill(a.skill_id) is not None]
    skipped = len(selected) - len(runnable)

    results = await asyncio.gather(
        *(_dispatch_one(conn, a, semaphore) for a in runnable)
    )

    effects: list[Any] = []
    dispatched_count = 0
    for act, (act_effects, outcome, usage_events, latency_ms, error) in zip(
        runnable, results, strict=True
    ):
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
            _record_observability(
                conn, run_id, field_id, act, outcome,
                len(act_effects), usage_events, latency_ms, now, error,
            )
    return effects, dispatched_count, skipped


async def _dispatch_one(
    conn: Any, act: Activation, semaphore: asyncio.Semaphore
) -> tuple[list[Any], DispatchOutcome, list[UsageEvent], int, tuple[str, str] | None]:
    """Run one activation's skill — ``fanout`` instances in parallel (a swarm on
    escalation) — and union their effects (deduped). Returns the effects, the
    dispatch outcome (effects / no_effects / error), the LLM usage this dispatch
    incurred, and its wall-clock latency (ms) — the last two feed the
    observability ledgers.

    A skill that raises contributes no effects and never aborts the round (the
    coordinator's one-bad-item philosophy). The outcome is ``error`` only when
    *every* instance raised, ``no_effects`` when they ran but produced nothing."""
    skill = get_skill(act.skill_id)
    assert skill is not None  # filtered by caller

    # Open a usage sink in THIS task's context before spawning the fanout tasks,
    # so every LLM call the skill makes (even across asyncio.to_thread) lands here
    # and nowhere else. Drained after the fanout completes.
    usage_sink = open_sink()
    start = time.monotonic()
    errors: list[Exception] = []

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
                errors.append(exc)
                return None

    batches = await asyncio.gather(*(run_once() for _ in range(act.fanout)))
    latency_ms = int((time.monotonic() - start) * 1000)
    ran = [b for b in batches if b is not None]
    if not ran:
        # Surface the failure cause so the invocation ledger records WHY (the
        # error_type/error_message columns), not just status=error.
        err = errors[0] if errors else None
        err_info = (type(err).__name__, str(err)[:1000]) if err is not None else None
        return [], DispatchOutcome.error, list(usage_sink), latency_ms, err_info

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
    return effects, outcome, list(usage_sink), latency_ms, None


def _record_observability(
    conn: Any,
    run_id: str,
    field_id: str,
    act: Activation,
    outcome: DispatchOutcome,
    n_effects: int,
    usage_events: list[UsageEvent],
    latency_ms: int,
    now: datetime,
    error: tuple[str, str] | None = None,
) -> None:
    """Persist one ``agent_invocations`` row per dispatch + one ``llm_usage`` row
    per LLM call it made. Best-effort: a ledger write must never abort the run
    (mirrors ``_record_run``). This is the only place the controller writes
    observability — the writers had no call sites after the LangGraph jobs were
    removed, which is why both tables sat empty. ``error`` (type, message) is
    recorded on the invocation row when the dispatch failed, so the ledger says
    WHY, not just that it errored."""
    try:
        in_tok = sum(
            u.input_tokens + u.cache_read_tokens + u.cache_creation_tokens
            for u in usage_events
        )
        out_tok = sum(u.output_tokens for u in usage_events)
        cost = sum(u.cost_usd for u in usage_events)
        # The realized model (a RoutedLLMClient may have escalated cheap→strong);
        # take the last call's model as representative for the invocation row.
        model = next((u.model for u in reversed(usage_events) if u.model), None)
        status = "error" if outcome == DispatchOutcome.error else "ok"
        create_agent_invocation(
            conn,
            AgentInvocation(
                run_id=run_id,
                field_id=field_id,
                agent=act.skill_id,
                skill=act.skill_id,
                status=status,
                error_type=error[0] if error else None,
                error_message=error[1] if error else None,
                model=model,
                latency_ms=latency_ms,
                input_tokens=in_tok or None,
                output_tokens=out_tok or None,
                cost_usd=round(cost, 6) if cost else None,
                output_summary={
                    "outcome": outcome.value,
                    "effects": n_effects,
                    "kind": act.tension.kind.value,
                    "tier": act.tension.tier.value,
                    "fanout": act.fanout,
                },
                created_at=now,
            ),
        )
        for u in usage_events:
            create_llm_usage(
                conn,
                LLMUsageRecord(
                    run_id=run_id,
                    skill_id=act.skill_id,
                    agent_name=act.skill_id,
                    model=u.model or None,
                    input_tokens=u.input_tokens,
                    output_tokens=u.output_tokens,
                    cache_read_tokens=u.cache_read_tokens,
                    cache_creation_tokens=u.cache_creation_tokens,
                    estimated_cost_usd=u.cost_usd,
                    created_at=now,
                ),
            )
    except Exception as exc:  # pragma: no cover - defensive ledger guard
        log.warning(
            "observability_record_failed", skill_id=act.skill_id, error=str(exc)
        )


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
@click.option(
    "--forever",
    "forever",
    is_flag=True,
    default=False,
    help="Run continuously as the self-driving orchestrator: sense → dispatch → "
    "apply on repeat, idling between empty passes. No external scheduler/cron. "
    "Implies --apply.",
)
def main(field: str, step_cap: int | None, apply_: bool, forever: bool) -> None:
    """Console entry point: `uv run mesh-controller` (shadow) / `--apply` (one live
    pass) / `--apply --forever` (self-driving daemon)."""
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ]
    )
    if forever:
        # Self-driving: never returns until the process is stopped.
        asyncio.run(run_controller_forever(field, step_cap=step_cap))
        return
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

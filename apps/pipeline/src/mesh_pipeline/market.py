"""Phase 1c of the agentic migration: the market loop (the new orchestrator).

This is the agentic replacement for the coordinator's fixed assembly line. Per
round it:

  1. scans the board into ranked candidate **tensions** (``compute_agenda``);
  2. lets every registered **skill** *bid* on the tensions it handles;
  3. **clears** the market — funds the highest value-per-dollar bids under the
     remaining budget (greedy knapsack);
  4. **dispatches** the funded tensions to their skills concurrently (bounded);
  5. applies the resulting **effects** through the write gateway;
  6. repeats until **quiescence** (a round that funds/changes nothing) or the
     budget runs out.

Strangler-fig: this lives *beside* ``coordinator.py``, not inside it. Before
Phase-2 skills are registered the market is a well-behaved no-op (it scans, finds
no skill to handle anything, and reports quiescence) — so it is safe to land and
run now. ``shadow=True`` (the default) collects the effects skills *would* emit
and reports them **without writing**, so you can diff the market's intentions
against the live store before letting it act; it runs a single round because the
board doesn't change when nothing is applied.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import click
import structlog
from mesh_agents.agenda import compute_agenda, scout_tensions
from mesh_agents.skill import Bid, Skill, load_builtin_skills, skills_for
from mesh_db.connection import get_connection
from mesh_db.effects import ApplyReport, apply_effects
from mesh_db.fields import get_field_by_slug
from mesh_db.pg_migrations import init_pg
from mesh_models.field import DEFAULT_FIELD_ID, DEFAULT_FIELD_SLUG
from mesh_models.tension import Tension
from pydantic import BaseModel, Field

log = structlog.get_logger()


def _get_concurrency() -> int:
    return int(os.environ.get("MESH_PIPELINE_CONCURRENCY", "3"))


class RoundReport(BaseModel):
    """What one market round saw and did."""

    round: int
    candidates: int  # tensions on the board this round
    funded: int  # tensions a skill bid on and the budget covered
    skipped_no_skill: int  # tensions no registered skill handles
    effects: int  # effects the funded skills emitted
    spent_usd: float
    apply: ApplyReport | None = None  # None in shadow mode (nothing written)


class MarketResult(BaseModel):
    run_id: str
    field_slug: str
    shadow: bool
    budget_usd: float
    rounds: list[RoundReport] = Field(default_factory=list)
    spent_usd: float = 0.0
    quiescent: bool = False  # stopped because there was nothing worth doing


class _Offer(BaseModel):
    tension: Tension
    bid: Bid
    skill_id: str

    model_config = {"arbitrary_types_allowed": True}


def _collect_offers(conn: Any, tensions: list[Tension]) -> tuple[list[_Offer], int]:
    """For each tension, the best bid among the skills that handle it. Returns
    ``(offers, skipped)`` where ``skipped`` counts tensions no skill handles."""
    offers: list[_Offer] = []
    skipped = 0
    for tension in tensions:
        candidates: list[tuple[Skill, Bid]] = []
        for skill in skills_for(tension.kind):
            bid = skill.bid(conn, tension)
            if bid is not None:
                candidates.append((skill, bid))
        if not candidates:
            skipped += 1
            continue
        skill, bid = max(candidates, key=lambda sb: sb[1].score)
        offers.append(_Offer(tension=tension, bid=bid, skill_id=skill.skill_id))
    return offers, skipped


def _clear(offers: list[_Offer], remaining_usd: float) -> list[_Offer]:
    """Greedy knapsack: fund highest value-per-dollar bids until the budget is gone."""
    funded: list[_Offer] = []
    spent = 0.0
    for offer in sorted(offers, key=lambda o: o.bid.score, reverse=True):
        if spent + offer.bid.est_cost_usd <= remaining_usd:
            funded.append(offer)
            spent += offer.bid.est_cost_usd
    return funded


async def run_market(
    field: str = DEFAULT_FIELD_SLUG,
    *,
    budget_usd: float = 0.50,
    shadow: bool = True,
    max_rounds: int = 25,
    conn: Any | None = None,
) -> MarketResult:
    """Run the market against one field until quiescence or budget exhaustion.

    ``shadow`` (default) writes nothing and runs a single round — a preview of the
    market's intentions. Set ``shadow=False`` to let it act and loop."""
    from mesh_agents.skill import all_skills

    load_builtin_skills()  # populate the registry (no-op until Phase 2)
    log.info("market_starting", field=field, shadow=shadow, skills=len(all_skills()))

    owns_conn = conn is None
    if conn is None:
        conn = get_connection()
        init_pg()
    field_row = get_field_by_slug(conn, field)
    field_id = field_row.id if field_row is not None else DEFAULT_FIELD_ID

    result = MarketResult(
        run_id=os.environ.get("MESH_RUN_ID") or str(uuid.uuid4()),
        field_slug=field,
        shadow=shadow,
        budget_usd=budget_usd,
    )
    semaphore = asyncio.Semaphore(_get_concurrency())
    # Once-per-run oscillation guard (Phase-3-lite): a tension dispatched this run
    # is not re-funded in a later round, even if it still derives from the board.
    # Tension ids are stable ("<kind>:<target>"), so this stops scout-source from
    # re-polling and investigate-gap from re-opening the same investigation every
    # round — and lets a multi-round run actually reach quiescence.
    dispatched: set[str] = set()

    try:
        for round_no in range(1, max_rounds + 1):
            remaining = budget_usd - result.spent_usd
            if remaining <= 0:
                break

            # 1. scan board → candidate tensions (big budget: we re-fund via bids),
            #    plus source-acquisition tensions (poll enabled connectors), minus
            #    anything already handled this run.
            agenda = compute_agenda(
                conn, field_id, field_slug=field, budget_usd=remaining * 1000
            )
            candidates = scout_tensions(conn, field_id) + agenda.tensions
            fresh = [t for t in candidates if t.id not in dispatched]
            if not fresh:
                result.quiescent = True
                break

            # 2-3. collect bids, clear under the remaining budget
            offers, skipped = _collect_offers(conn, fresh)
            funded = _clear(offers, remaining)

            # 4. dispatch funded tensions to their skills (bounded concurrency)
            effects = await _dispatch(conn, funded, semaphore)
            for offer in funded:
                dispatched.add(offer.tension.id)
            round_spent = round(sum(o.bid.est_cost_usd for o in funded), 4)

            # 5. apply (or, in shadow, only report)
            apply_report: ApplyReport | None = None
            if not shadow and effects:
                apply_report = apply_effects(conn, effects)

            result.rounds.append(
                RoundReport(
                    round=round_no,
                    candidates=len(fresh),
                    funded=len(funded),
                    skipped_no_skill=skipped,
                    effects=len(effects),
                    spent_usd=round_spent,
                    apply=apply_report,
                )
            )
            result.spent_usd = round(result.spent_usd + round_spent, 4)
            log.info(
                "market_round",
                round=round_no,
                candidates=len(fresh),
                funded=len(funded),
                effects=len(effects),
                skipped_no_skill=skipped,
            )

            # 6. stop conditions: shadow previews one round; live stops when a
            # round made no progress (nothing funded → board can't change).
            if shadow or not funded:
                if not funded:
                    result.quiescent = True
                break
    finally:
        if owns_conn:
            conn.close()

    log.info(
        "market_complete",
        field=field,
        rounds=len(result.rounds),
        spent_usd=result.spent_usd,
        quiescent=result.quiescent,
    )
    return result


async def _dispatch(
    conn: Any, funded: list[_Offer], semaphore: asyncio.Semaphore
) -> list[Any]:
    """Run each funded tension's skill concurrently; flatten their effects. A
    skill that raises is recorded and contributes no effects (never aborts the
    round) — the coordinator's one-bad-item-never-fails philosophy."""
    from mesh_agents.skill import get_skill

    async def run_one(offer: _Offer) -> list[Any]:
        skill = get_skill(offer.skill_id)
        if skill is None:
            return []
        async with semaphore:
            try:
                return await skill.run(
                    conn, offer.tension, budget_usd=offer.bid.est_cost_usd
                )
            except Exception as exc:
                log.warning(
                    "skill_run_failed",
                    skill_id=offer.skill_id,
                    tension=offer.tension.id,
                    error=str(exc),
                )
                return []

    batches = await asyncio.gather(*(run_one(o) for o in funded))
    return [effect for batch in batches for effect in batch]


@click.command()
@click.option("--field", default=DEFAULT_FIELD_SLUG, show_default=True)
@click.option("--budget", "budget_usd", default=0.50, type=float, show_default=True)
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    default=False,
    help="Let the market ACT (write through the gateway + loop). Default is "
    "shadow mode: preview one round's intentions, write nothing.",
)
def main(field: str, budget_usd: float, apply_: bool) -> None:
    """Console entry point: `uv run mesh-market` (shadow) / `--apply` (live)."""
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ]
    )
    result = asyncio.run(run_market(field, budget_usd=budget_usd, shadow=not apply_))
    mode = "LIVE" if apply_ else "SHADOW"
    print(f"\nMarket {result.run_id} [{result.field_slug}] — {mode}")
    print(f"  Budget:    ${result.budget_usd:.2f}   Spent: ${result.spent_usd:.3f}")
    print(f"  Rounds:    {len(result.rounds)}   Quiescent: {result.quiescent}")
    for r in result.rounds:
        print(
            f"  round {r.round}: {r.candidates} candidates, {r.funded} funded, "
            f"{r.skipped_no_skill} unhandled, {r.effects} effects"
        )
    if not result.rounds or all(r.skipped_no_skill == r.candidates for r in result.rounds):
        print("  (no skills registered yet — expected before Phase 2 fan-out)")

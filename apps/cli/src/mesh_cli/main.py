from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import click
from mesh_db.beliefs import create_belief, get_belief_by_id, list_beliefs, update_belief
from mesh_db.claims import create_claim, get_claim_by_id, list_claims
from mesh_db.connection import get_connection
from mesh_db.entities import create_entity, get_entity_by_id, list_entities
from mesh_db.heuristics import list_heuristics
from mesh_db.investigations import get_investigation_by_id, list_investigations
from mesh_db.llm_usage import aggregate_usage_by_model, aggregate_usage_by_skill
from mesh_db.pg_migrations import init_pg
from mesh_db.pipeline_runs import list_pipeline_runs
from mesh_db.relationships import get_relationship_by_id
from mesh_db.revisions import create_revision, get_revision_by_id, list_revisions
from mesh_db.sources import create_source, get_source_by_id, list_sources
from mesh_models.belief import Belief
from mesh_models.claim import Claim, ClaimStatus
from mesh_models.entity import Entity, EntityType
from mesh_models.investigation import (
    Investigation,
    InvestigationOrigin,
    InvestigationStatus,
)
from mesh_models.revision import BeliefRevision
from mesh_models.source import Source, SourceType
from rich.console import Console
from rich.table import Table

console = Console()


def _get_conn() -> Any:
    return get_connection()


@click.group()
def cli() -> None:
    """Agent Mesh CLI — manage the research knowledge base."""


@cli.command("init-db")
def init_db() -> None:
    """Apply the Postgres knowledge schema + roles. Idempotent.

    Uses MESH_PG_URL / LANGGRAPH_POSTGRES_URL; run as a superuser/DB owner so
    CREATE EXTENSION + CREATE ROLE succeed.
    """
    applied = init_pg()
    if applied:
        console.print(
            f"[green]Knowledge schema initialized.[/green] Applied: {', '.join(applied)}"
        )
    else:
        console.print("[green]Knowledge schema already up to date.[/green]")


@cli.command("backfill-claim-types")
def backfill_claim_types_cmd() -> None:
    """Type any untyped/drifted claims (Phase 14a). Deterministic + idempotent.

    Migration 007 backfills claim_type on apply; this re-runnable command
    recomputes claim_type from each claim's predicate for any rows where it is
    NULL or has drifted (e.g. after a manual edit). No LLM — the predicate fully
    determines the claim_type.
    """
    from mesh_db.claims import backfill_claim_types

    conn = get_connection()  # writer
    try:
        updated = backfill_claim_types(conn)
    finally:
        conn.close()
    console.print(f"[green]Claim types backfilled: {updated} row(s) updated.[/green]")


_ENTITY_CHOICES = click.Choice([e.value for e in EntityType])
_SOURCE_CHOICES = click.Choice([s.value for s in SourceType])
_CLAIM_STATUS_CHOICES = click.Choice([c.value for c in ClaimStatus])


@cli.command("add-entity")
@click.option("--name", required=True, help="Canonical name")
@click.option("--type", "entity_type", required=True, type=_ENTITY_CHOICES)
@click.option("--alias", "aliases", multiple=True, help="Additional aliases")
@click.option("--attribute", "attributes", multiple=True, metavar="KEY=VALUE")
def add_entity(
    name: str, entity_type: str, aliases: tuple[str, ...], attributes: tuple[str, ...]
) -> None:
    """Create a new entity."""
    attrs: dict[str, str] = {}
    for kv in attributes:
        if "=" not in kv:
            raise click.BadParameter(f"Attribute must be KEY=VALUE, got: {kv}")
        k, _, v = kv.partition("=")
        attrs[k] = v

    entity = Entity(
        canonical_name=name,
        aliases=list(aliases),
        type=EntityType(entity_type),
        attributes=attrs,
    )
    conn = _get_conn()
    create_entity(conn, entity)
    conn.close()
    _print_entity(entity)


@cli.command("add-source")
@click.option("--type", "source_type", required=True, type=_SOURCE_CHOICES)
@click.option("--url", required=True)
@click.option("--author", default=None)
@click.option("--published-at", default=None, help="ISO-8601 datetime")
def add_source(source_type: str, url: str, author: str | None, published_at: str | None) -> None:
    """Create a new source."""
    pub_dt = datetime.fromisoformat(published_at) if published_at else datetime.now(UTC)
    source = Source(
        type=SourceType(source_type),
        url=url,
        author=author,
        published_at=pub_dt,
        raw_content_hash=hashlib.sha256(url.encode()).hexdigest(),
    )
    conn = _get_conn()
    create_source(conn, source)
    conn.close()
    _print_source(source)


@cli.command("add-claim")
@click.option("--subject", "subject_id", required=True, help="Entity ID")
@click.option("--predicate", required=True)
@click.option("--object", "object_json", required=True, help="JSON string")
@click.option("--source", "source_id", required=True)
@click.option("--agent", default="cli", show_default=True)
@click.option("--excerpt", default="", show_default=True)
@click.option("--confidence", default=0.5, type=float, show_default=True)
def add_claim(
    subject_id: str,
    predicate: str,
    object_json: str,
    source_id: str,
    agent: str,
    excerpt: str,
    confidence: float,
) -> None:
    """Create a new claim."""
    try:
        obj: dict[str, Any] = json.loads(object_json)
    except json.JSONDecodeError as e:
        raise click.BadParameter(f"--object must be valid JSON: {e}") from e

    claim = Claim(
        predicate=predicate,
        subject_entity_id=subject_id,
        object=obj,
        source_id=source_id,
        extracted_by_agent=agent,
        raw_excerpt=excerpt,
        confidence=confidence,
    )
    conn = _get_conn()
    create_claim(conn, claim)
    conn.close()
    _print_claim(claim)


@cli.command("add-belief")
@click.option("--topic", required=True)
@click.option("--statement", required=True)
@click.option("--supporting", "supporting_ids", multiple=True, metavar="CLAIM_ID")
@click.option("--contradicting", "contradicting_ids", multiple=True, metavar="CLAIM_ID")
@click.option("--confidence", default=0.5, type=float)
def add_belief(
    topic: str,
    statement: str,
    supporting_ids: tuple[str, ...],
    contradicting_ids: tuple[str, ...],
    confidence: float,
) -> None:
    """Create a new belief."""
    belief = Belief(
        topic=topic,
        statement=statement,
        supporting_claim_ids=list(supporting_ids),
        contradicting_claim_ids=list(contradicting_ids),
        confidence=confidence,
    )
    conn = _get_conn()
    create_belief(conn, belief)
    conn.close()
    _print_belief(belief)


@cli.command("add-revision")
@click.option("--belief", "belief_id", required=True)
@click.option("--new-statement", required=True)
@click.option("--new-confidence", required=True, type=float)
@click.option("--rationale", required=True)
@click.option("--trigger", "trigger_ids", multiple=True, metavar="CLAIM_ID")
@click.option("--agent", default="cli", show_default=True)
def add_revision(
    belief_id: str,
    new_statement: str,
    new_confidence: float,
    rationale: str,
    trigger_ids: tuple[str, ...],
    agent: str,
) -> None:
    """Revise an existing belief."""
    conn = _get_conn()
    belief = get_belief_by_id(conn, belief_id)
    if belief is None:
        conn.close()
        raise click.ClickException(f"Belief {belief_id} not found")

    revision = BeliefRevision(
        belief_id=belief_id,
        previous_statement=belief.statement,
        new_statement=new_statement,
        previous_confidence=belief.confidence,
        new_confidence=new_confidence,
        trigger_claim_ids=list(trigger_ids),
        revised_by_agent=agent,
        rationale=rationale,
    )
    create_revision(conn, revision)
    update_belief(
        conn,
        belief_id,
        statement=new_statement,
        confidence=new_confidence,
        last_revised_at=revision.revised_at,
        revision_count=belief.revision_count + 1,
    )
    conn.close()
    _print_revision(revision)


@cli.command("show-entities")
@click.option("--type", "entity_type", default=None, type=_ENTITY_CHOICES)
@click.option("--limit", default=50, type=int)
def show_entities(entity_type: str | None, limit: int) -> None:
    """List entities."""
    conn = _get_conn()
    etype = EntityType(entity_type) if entity_type else None
    entities = list_entities(conn, type=etype, limit=limit)
    conn.close()

    table = Table(title="Entities", show_lines=False)
    table.add_column("ID", style="dim", max_width=36)
    table.add_column("Name")
    table.add_column("Type", style="cyan")
    table.add_column("Aliases")
    for e in entities:
        table.add_row(e.id, e.canonical_name, e.type.value, ", ".join(e.aliases))
    console.print(table)


@cli.command("show-sources")
@click.option("--type", "source_type", default=None, type=_SOURCE_CHOICES)
@click.option("--limit", default=50, type=int)
def show_sources(source_type: str | None, limit: int) -> None:
    """List sources."""
    conn = _get_conn()
    stype = SourceType(source_type) if source_type else None
    sources = list_sources(conn, type=stype, limit=limit)
    conn.close()

    table = Table(title="Sources")
    table.add_column("ID", style="dim", max_width=36)
    table.add_column("Type", style="cyan")
    table.add_column("URL")
    table.add_column("Author")
    for s in sources:
        table.add_row(s.id, s.type.value, s.url, s.author or "")
    console.print(table)


@cli.command("show-claims")
@click.option("--entity", "entity_id", default=None)
@click.option("--source", "source_id", default=None)
@click.option("--status", default=None, type=_CLAIM_STATUS_CHOICES)
@click.option("--limit", default=50, type=int)
def show_claims(
    entity_id: str | None, source_id: str | None, status: str | None, limit: int
) -> None:
    """List claims."""
    conn = _get_conn()
    cstatus = ClaimStatus(status) if status else None
    claims = list_claims(
        conn, entity_id=entity_id, source_id=source_id, status=cstatus, limit=limit
    )
    conn.close()

    table = Table(title="Claims")
    table.add_column("ID", style="dim", max_width=36)
    table.add_column("Predicate")
    table.add_column("Status", style="cyan")
    table.add_column("Confidence")
    for c in claims:
        table.add_row(c.id, c.predicate, c.status.value, f"{c.confidence:.2f}")
    console.print(table)


@cli.command("show-beliefs")
@click.option("--topic", default=None)
@click.option("--currently-held", is_flag=True, default=False)
@click.option("--limit", default=50, type=int)
def show_beliefs(topic: str | None, currently_held: bool, limit: int) -> None:
    """List beliefs."""
    conn = _get_conn()
    held: bool | None = True if currently_held else None
    beliefs = list_beliefs(conn, topic=topic, currently_held=held, limit=limit)
    conn.close()

    table = Table(title="Beliefs")
    table.add_column("ID", style="dim", max_width=36)
    table.add_column("Topic", style="cyan")
    table.add_column("Statement")
    table.add_column("Confidence")
    table.add_column("Held")
    for b in beliefs:
        table.add_row(
            b.id, b.topic, b.statement[:60], f"{b.confidence:.2f}",
            "yes" if b.is_currently_held else "no",
        )
    console.print(table)


@cli.command("show-revisions")
@click.option("--belief", "belief_id", required=True)
def show_revisions(belief_id: str) -> None:
    """List revisions for a belief."""
    conn = _get_conn()
    revisions = list_revisions(conn, belief_id=belief_id)
    conn.close()

    table = Table(title=f"Revisions for belief {belief_id[:8]}…")
    table.add_column("ID", style="dim", max_width=36)
    table.add_column("Agent")
    table.add_column("Confidence", justify="right")
    table.add_column("Rationale")
    for r in revisions:
        conf_delta = f"{r.previous_confidence:.2f} → {r.new_confidence:.2f}"
        table.add_row(r.id, r.revised_by_agent, conf_delta, r.rationale[:60])
    console.print(table)


@cli.command("inspect")
@click.argument("id")
def inspect(id: str) -> None:
    """Auto-detect and pretty-print any record by ID."""
    conn = _get_conn()

    entity = get_entity_by_id(conn, id)
    if entity is not None:
        conn.close()
        _print_entity_detail(entity)
        return

    source = get_source_by_id(conn, id)
    if source is not None:
        conn.close()
        _print_source_detail(source)
        return

    claim = get_claim_by_id(conn, id)
    if claim is not None:
        entity_ref = get_entity_by_id(conn, claim.subject_entity_id)
        source_ref = get_source_by_id(conn, claim.source_id)
        conn.close()
        _print_claim_detail(claim, entity_ref, source_ref)
        return

    belief = get_belief_by_id(conn, id)
    if belief is not None:
        conn.close()
        _print_belief_detail(belief)
        return

    revision = get_revision_by_id(conn, id)
    if revision is not None:
        conn.close()
        _print_revision_detail(revision)
        return

    investigation = get_investigation_by_id(conn, id)
    if investigation is not None:
        conn.close()
        _print_investigation_detail(investigation)
        return

    relationship = get_relationship_by_id(conn, id)
    if relationship is not None:
        conn.close()
        _print_relationship_detail(relationship)
        return

    conn.close()
    raise click.ClickException(f"No record found with ID: {id}")


# --- pretty-print helpers ---

def _print_entity(e: Entity) -> None:
    console.print(f"[bold]Entity[/bold] [dim]{e.id}[/dim]")
    console.print(f"  name: {e.canonical_name}  type: [cyan]{e.type.value}[/cyan]")


def _print_source(s: Source) -> None:
    console.print(f"[bold]Source[/bold] [dim]{s.id}[/dim]")
    console.print(f"  type: [cyan]{s.type.value}[/cyan]  url: {s.url}")


def _print_claim(c: Claim) -> None:
    console.print(f"[bold]Claim[/bold] [dim]{c.id}[/dim]")
    console.print(
        f"  predicate: {c.predicate}  status: [cyan]{c.status.value}[/cyan]"
        f"  confidence: {c.confidence:.2f}"
    )


def _print_belief(b: Belief) -> None:
    console.print(f"[bold]Belief[/bold] [dim]{b.id}[/dim]")
    console.print(f"  topic: {b.topic}  confidence: {b.confidence:.2f}")
    console.print(f"  {b.statement}")


def _print_revision(r: BeliefRevision) -> None:
    console.print(f"[bold]Revision[/bold] [dim]{r.id}[/dim]")
    console.print(f"  belief: {r.belief_id}  agent: {r.revised_by_agent}")
    console.print(f"  confidence: {r.previous_confidence:.2f} → {r.new_confidence:.2f}")


def _print_entity_detail(e: Entity) -> None:
    from rich.panel import Panel

    lines = [
        f"[bold]ID:[/bold] {e.id}",
        f"[bold]Name:[/bold] {e.canonical_name}",
        f"[bold]Type:[/bold] [cyan]{e.type.value}[/cyan]",
        f"[bold]Aliases:[/bold] {', '.join(e.aliases) or '—'}",
        f"[bold]Attributes:[/bold] {json.dumps(e.attributes, indent=2)}",
        f"[bold]Created:[/bold] {e.created_at.isoformat()}",
        f"[bold]Last seen:[/bold] {e.last_seen_at.isoformat()}",
    ]
    console.print(Panel("\n".join(lines), title="Entity"))


def _print_source_detail(s: Source) -> None:
    from rich.panel import Panel

    lines = [
        f"[bold]ID:[/bold] {s.id}",
        f"[bold]Type:[/bold] [cyan]{s.type.value}[/cyan]",
        f"[bold]URL:[/bold] {s.url}",
        f"[bold]Author:[/bold] {s.author or '—'}",
        f"[bold]Published:[/bold] {s.published_at.isoformat()}",
        f"[bold]Fetched:[/bold] {s.fetched_at.isoformat()}",
        f"[bold]Reliability:[/bold] {s.reliability_prior:.2f}",
    ]
    console.print(Panel("\n".join(lines), title="Source"))


def _print_claim_detail(
    c: Claim,
    entity: Entity | None,
    source: Source | None,
) -> None:
    from rich.panel import Panel

    entity_str = f"{entity.canonical_name} ({entity.id})" if entity else c.subject_entity_id
    source_str = f"{source.url} ({source.id})" if source else c.source_id
    lines = [
        f"[bold]ID:[/bold] {c.id}",
        f"[bold]Predicate:[/bold] {c.predicate}",
        f"[bold]Subject:[/bold] {entity_str}",
        f"[bold]Object:[/bold] {json.dumps(c.object, indent=2)}",
        f"[bold]Source:[/bold] {source_str}",
        f"[bold]Status:[/bold] [cyan]{c.status.value}[/cyan]",
        f"[bold]Confidence:[/bold] {c.confidence:.2f}",
        f"[bold]Excerpt:[/bold] {c.raw_excerpt}",
        f"[bold]Agent:[/bold] {c.extracted_by_agent}",
    ]
    console.print(Panel("\n".join(lines), title="Claim"))


def _print_belief_detail(b: Belief) -> None:
    from rich.panel import Panel

    lines = [
        f"[bold]ID:[/bold] {b.id}",
        f"[bold]Topic:[/bold] {b.topic}",
        f"[bold]Statement:[/bold] {b.statement}",
        f"[bold]Confidence:[/bold] {b.confidence:.2f}",
        f"[bold]Currently held:[/bold] {'yes' if b.is_currently_held else 'no'}",
        f"[bold]Supporting:[/bold] {', '.join(b.supporting_claim_ids) or '—'}",
        f"[bold]Contradicting:[/bold] {', '.join(b.contradicting_claim_ids) or '—'}",
        f"[bold]Revisions:[/bold] {b.revision_count}",
        f"[bold]Last revised:[/bold] {b.last_revised_at.isoformat()}",
    ]
    console.print(Panel("\n".join(lines), title="Belief"))


def _print_revision_detail(r: BeliefRevision) -> None:
    from rich.panel import Panel

    lines = [
        f"[bold]ID:[/bold] {r.id}",
        f"[bold]Belief:[/bold] {r.belief_id}",
        f"[bold]Agent:[/bold] {r.revised_by_agent}",
        f"[bold]Previous statement:[/bold] {r.previous_statement}",
        f"[bold]New statement:[/bold] {r.new_statement}",
        f"[bold]Confidence:[/bold] {r.previous_confidence:.2f} → {r.new_confidence:.2f}",
        f"[bold]Triggers:[/bold] {', '.join(r.trigger_claim_ids) or '—'}",
        f"[bold]Rationale:[/bold] {r.rationale}",
        f"[bold]Revised at:[/bold] {r.revised_at.isoformat()}",
    ]
    console.print(Panel("\n".join(lines), title="BeliefRevision"))


def _print_investigation_detail(inv: Investigation) -> None:
    from rich.panel import Panel

    lines = [
        f"[bold]ID:[/bold] {inv.id}",
        f"[bold]Question:[/bold] {inv.question}",
        f"[bold]Status:[/bold] [cyan]{inv.status.value}[/cyan]",
        f"[bold]Priority:[/bold] {inv.priority:.2f}",
        f"[bold]Entities:[/bold] {', '.join(inv.related_entity_ids) or '—'}",
        f"[bold]Agents:[/bold] {', '.join(inv.assigned_scout_agents) or '—'}",
        f"[bold]Created:[/bold] {inv.created_at.isoformat()}",
        f"[bold]Resolved:[/bold] {inv.resolved_at.isoformat() if inv.resolved_at else '—'}",
    ]
    console.print(Panel("\n".join(lines), title="Investigation"))


@cli.command("pipeline-stats")
@click.option("--last", default=10, type=int, show_default=True, help="Show last N runs")
def pipeline_stats(last: int) -> None:
    """Show recent pipeline run statistics."""
    conn = _get_conn()
    try:
        runs = list_pipeline_runs(conn, limit=last)
    except Exception:
        console.print("[yellow]No pipeline_runs table yet — run mesh.cli init-db first.[/yellow]")
        conn.close()
        return
    conn.close()

    if not runs:
        console.print("[dim]No pipeline runs recorded yet.[/dim]")
        return

    table = Table(title="Pipeline Runs")
    table.add_column("ID", style="dim", max_width=8)
    table.add_column("Started", style="cyan")
    table.add_column("Sources")
    table.add_column("Claims")
    table.add_column("Entities")
    table.add_column("Beliefs +/~")
    table.add_column("Avg LLM ms")
    table.add_column("Errors")
    for r in runs:
        table.add_row(
            r.id[:8],
            r.started_at.strftime("%Y-%m-%d %H:%M"),
            str(r.sources_inserted),
            str(r.claims_inserted),
            str(r.entities_created),
            f"+{r.beliefs_created}/~{r.beliefs_revised}",
            str(r.avg_extraction_latency_ms),
            str(len(r.errors)),
        )
    console.print(table)


@cli.group("cost")
def cost() -> None:
    """LLM token + cost reporting (Phase 11)."""


def _fmt_usd(amount: float) -> str:
    return f"${amount:,.4f}"


@cost.command("report")
@click.option("--run-id", default=None, help="Report a specific pipeline/sweep run id")
@click.option(
    "--last", default=1, type=int, show_default=True,
    help="Report the most recent N runs (ignored when --run-id is given)",
)
def cost_report(run_id: str | None, last: int) -> None:
    """Per-skill LLM token totals and estimated cost for a run.

    Costs are the values recorded in the llm_usage ledger at run time, which
    already account for cache reads (11c) and the batch discount (11d).
    """
    conn = _get_conn()
    try:
        if run_id is not None:
            runs = [r for r in list_pipeline_runs(conn, limit=1000) if r.id == run_id]
            if not runs:
                console.print(f"[yellow]No run found with id {run_id}.[/yellow]")
                return
        else:
            runs = list_pipeline_runs(conn, limit=last)
        if not runs:
            console.print("[dim]No pipeline runs recorded yet.[/dim]")
            return

        grand_total = 0.0
        for run in runs:
            totals = aggregate_usage_by_skill(conn, run.id)
            title = (
                f"{run.run_type} {run.id[:8]} — "
                f"{run.started_at.strftime('%Y-%m-%d %H:%M')} ({run.triggered_by})"
            )
            table = Table(title=title)
            table.add_column("Skill", style="cyan")
            table.add_column("Calls", justify="right")
            table.add_column("Input", justify="right")
            table.add_column("Output", justify="right")
            table.add_column("Cache R/W", justify="right")
            table.add_column("Model", style="dim")
            table.add_column("Cost", justify="right")

            run_cost = 0.0
            for t in totals:
                # Use the cost recorded at run time: it already reflects the
                # cache discount (11c) and the batch discount (11d), which a
                # token-only recompute here could not know about.
                skill_cost = t.estimated_cost_usd
                run_cost += skill_cost
                table.add_row(
                    t.skill_id,
                    str(t.calls),
                    f"{t.input_tokens:,}",
                    f"{t.output_tokens:,}",
                    f"{t.cache_read_tokens:,}/{t.cache_creation_tokens:,}",
                    t.model or "—",
                    _fmt_usd(skill_cost),
                )
            if not totals:
                table.add_row("[dim]no LLM calls recorded[/dim]", "", "", "", "", "", "")
            table.add_section()
            table.add_row(
                "[bold]TOTAL[/bold]", "", "", "", "", "",
                f"[bold]{_fmt_usd(run_cost)}[/bold]",
            )
            console.print(table)
            grand_total += run_cost

        if len(runs) > 1:
            console.print(
                f"\n[bold]Grand total across {len(runs)} runs: "
                f"{_fmt_usd(grand_total)}[/bold]"
            )
    finally:
        conn.close()


def _parse_since(since: str | None) -> datetime | None:
    """Parse a ``--since`` value: ``24h`` / ``7d`` durations or an ISO date."""
    if since is None:
        return None
    if since.endswith("h"):
        return datetime.now(UTC) - timedelta(hours=int(since[:-1]))
    if since.endswith("d"):
        return datetime.now(UTC) - timedelta(days=int(since[:-1]))
    return datetime.fromisoformat(since)


def _tier_for_model(model: str | None) -> str:
    """Label a realized model with its routing tier.

    Tier isn't persisted (the ledger records only the realized ``model``), so
    derive it: match the live RoutingConfig's cheap/strong models first, then
    fall back to a family heuristic. Unpriced/local models read as cheap.
    """
    if not model:
        return "—"
    from mesh_llm.routing import RoutingConfig

    cfg = RoutingConfig.from_env()
    if model == cfg.cheap_model:
        return "cheap"
    if model == cfg.strong_model:
        return "strong"
    # Family fallback for models that don't match the current config exactly.
    if model.startswith(("claude-sonnet", "claude-opus", "claude-3-5-sonnet")):
        return "strong"
    if model.startswith(("claude-haiku", "claude-3-5-haiku")):
        return "cheap"
    return "?"


@cli.command("routing-stats")
@click.option("--field", "field_slug", default=None, help="Scope to a field slug")
@click.option(
    "--since", default=None,
    help="Only usage since this date/duration (e.g. 24h, 7d, 2024-01-01)",
)
def routing_stats(field_slug: str | None, since: str | None) -> None:
    """Per-tier LLM request / token / cost split from the llm_usage ledger.

    The before/after evidence that tiered routing (Phase 20) is paying off:
    aggregates the realized model recorded per call, labels each with its tier,
    and shows request counts, token totals, and estimated dollars per tier.
    Reads the existing ledger — no new table.
    """
    try:
        since_dt = _parse_since(since)
    except ValueError as exc:
        console.print(f"[red]Invalid --since value:[/red] {exc}")
        return

    conn = _get_conn()
    try:
        totals = aggregate_usage_by_model(
            conn, field_id=field_slug, since=since_dt
        )
    except Exception:
        console.print(
            "[yellow]No llm_usage ledger yet — run mesh.cli init-db and a "
            "pipeline first.[/yellow]"
        )
        conn.close()
        return
    conn.close()

    if not totals:
        console.print("[dim]No LLM usage recorded for that scope.[/dim]")
        return

    scope = field_slug or "all fields"
    window = f" since {since}" if since else ""
    table = Table(title=f"Routing stats — {scope}{window}")
    table.add_column("Tier", style="cyan")
    table.add_column("Model", style="dim")
    table.add_column("Calls", justify="right")
    table.add_column("Input", justify="right")
    table.add_column("Output", justify="right")
    table.add_column("Cost", justify="right")

    tier_calls: dict[str, int] = {}
    tier_cost: dict[str, float] = {}
    grand_calls = 0
    grand_cost = 0.0
    # Sort by tier (cheap, strong, then the rest) so the split reads top-down.
    tier_order = {"cheap": 0, "strong": 1}
    rows = sorted(
        totals,
        key=lambda t: (tier_order.get(_tier_for_model(t.model), 2), -t.estimated_cost_usd),
    )
    for t in rows:
        tier = _tier_for_model(t.model)
        tier_calls[tier] = tier_calls.get(tier, 0) + t.calls
        tier_cost[tier] = tier_cost.get(tier, 0.0) + t.estimated_cost_usd
        grand_calls += t.calls
        grand_cost += t.estimated_cost_usd
        table.add_row(
            tier,
            t.model or "—",
            str(t.calls),
            f"{t.input_tokens:,}",
            f"{t.output_tokens:,}",
            _fmt_usd(t.estimated_cost_usd),
        )

    table.add_section()
    for tier in sorted(tier_calls, key=lambda k: tier_order.get(k, 2)):
        share = (tier_calls[tier] / grand_calls * 100) if grand_calls else 0.0
        table.add_row(
            f"[bold]{tier}[/bold]",
            f"[dim]{share:.0f}% of calls[/dim]",
            f"[bold]{tier_calls[tier]}[/bold]",
            "",
            "",
            f"[bold]{_fmt_usd(tier_cost[tier])}[/bold]",
        )
    table.add_section()
    table.add_row(
        "[bold]TOTAL[/bold]", "", f"[bold]{grand_calls}[/bold]", "", "",
        f"[bold]{_fmt_usd(grand_cost)}[/bold]",
    )
    console.print(table)


@cli.command("show-recent-claims")
@click.option("--limit", default=20, type=int, show_default=True)
def show_recent_claims(limit: int) -> None:
    """List recent claims with entity name and source URL inline."""
    conn = _get_conn()
    claims = list_claims(conn, limit=limit)

    table = Table(title="Recent Claims")
    table.add_column("Predicate", style="cyan")
    table.add_column("Subject")
    table.add_column("Confidence", justify="right")
    table.add_column("Source URL")
    for c in claims:
        entity = get_entity_by_id(conn, c.subject_entity_id)
        source = get_source_by_id(conn, c.source_id)
        table.add_row(
            c.predicate,
            entity.canonical_name if entity else c.subject_entity_id[:8],
            f"{c.confidence:.2f}",
            (source.url[:60] if source else ""),
        )
    conn.close()
    console.print(table)


@cli.command("show-sota-beliefs")
def show_sota_beliefs() -> None:
    """List all current SOTA beliefs (topic prefix sota:)."""
    conn = _get_conn()
    beliefs = list_beliefs(conn, topic="sota:", currently_held=True, limit=200)
    conn.close()

    if not beliefs:
        console.print("[dim]No SOTA beliefs recorded yet. Run mesh-pipeline first.[/dim]")
        return

    table = Table(title="SOTA Beliefs")
    table.add_column("Benchmark", style="cyan")
    table.add_column("Statement")
    table.add_column("Confidence", justify="right")
    table.add_column("Revisions", justify="right")
    for b in beliefs:
        benchmark = b.topic.removeprefix("sota:")
        table.add_row(benchmark, b.statement[:70], f"{b.confidence:.2f}", str(b.revision_count))
    console.print(table)


@cli.command("ask")
@click.argument("question")
@click.option("--field", default="ai-robotics", help="Field slug to scope the answer to")
def ask(question: str, field: str) -> None:
    """Ask a grounded question about a field's knowledge graph.

    Retrieves field-scoped beliefs/claims/entities for QUESTION and synthesizes
    a cited answer using only that evidence. Runs the ResearchQA agent in-process
    against a read-only connection; nothing is written.
    """
    import asyncio

    from mesh_agents.research_qa import ResearchQAAgent, ResearchQAInput
    from mesh_llm import LLMProviderNotReadyError, make_routed_llm_client
    from mesh_models.qa import Coverage
    from rich.panel import Panel

    q = question.strip()
    if not q:
        console.print("[red]Question must not be empty.[/red]")
        raise SystemExit(1)

    try:
        llm = make_routed_llm_client(agent_name="research_qa")
        llm.health_check()
    except LLMProviderNotReadyError as exc:
        console.print(f"[red]LLM provider not ready: {exc}[/red]")
        raise SystemExit(1) from exc

    conn = get_connection(read_only=True)
    agent = ResearchQAAgent(llm=llm, db_conn=conn)
    try:
        answer = asyncio.run(agent.run(ResearchQAInput(question=q, field_id=field)))
    finally:
        conn.close()

    coverage_style = {
        Coverage.well_supported: "green",
        Coverage.thin: "yellow",
        Coverage.uncovered: "red",
    }.get(answer.coverage, "white")
    console.print(
        Panel(
            answer.answer_markdown,
            title=f"Answer · [{coverage_style}]{answer.coverage.value}[/{coverage_style}]",
        )
    )
    if answer.citations:
        table = Table(title="Citations")
        table.add_column("Kind", style="cyan")
        table.add_column("ID", style="dim")
        table.add_column("Quote")
        for c in answer.citations:
            table.add_row(c.kind, c.id, c.quote)
        console.print(table)
    if answer.caveats:
        console.print("[bold]Caveats:[/bold]")
        for cav in answer.caveats:
            console.print(f"  • {cav}")


@cli.command("ollama-check")
def ollama_check() -> None:
    """Ping Ollama and verify the configured model is available."""
    from mesh_llm.client import OllamaClient, OllamaNotReadyError

    client = OllamaClient()
    console.print(f"Host:  [cyan]{client.host}[/cyan]")
    console.print(f"Model: [cyan]{client.model}[/cyan]")

    try:
        models_response = client._client.list()
        available = [m.model for m in models_response.models]
        console.print(f"[green]Ollama is running.[/green]  Available models: {available}")
        client.health_check()
        console.print(f"[green]Model '{client.model}' is available.[/green]")
    except OllamaNotReadyError as exc:
        console.print(f"[red]{exc}[/red]")
    except Exception as exc:
        console.print(f"[red]Ollama not reachable: {exc}[/red]")


@cli.command("a2a-discover")
@click.option(
    "--agent-urls",
    default=None,
    envvar="MESH_AGENT_URLS",
    help="Comma-separated agent base URLs (default: localhost ports 8001-8004)",
)
def a2a_discover(agent_urls: str | None) -> None:
    """Fetch A2A agent cards and print a discovery table."""
    import asyncio

    import httpx
    from a2a.client.card_resolver import A2ACardResolver

    _DEFAULT_URLS = [
        "http://localhost:8001",
        "http://localhost:8002",
        "http://localhost:8003",
        "http://localhost:8004",
    ]
    urls = [u.strip() for u in agent_urls.split(",")] if agent_urls else _DEFAULT_URLS

    table = Table(title="Discovered A2A Agents")
    table.add_column("URL")
    table.add_column("Name")
    table.add_column("Version")
    table.add_column("Skills")
    table.add_column("Streaming")

    async def _discover() -> None:
        async with httpx.AsyncClient(timeout=5.0) as http:
            for url in urls:
                try:
                    resolver = A2ACardResolver(http, url)
                    card = await resolver.get_agent_card()
                    skill_ids = ", ".join(s.id for s in card.skills)
                    table.add_row(
                        url,
                        card.name,
                        card.version,
                        skill_ids,
                        str(card.capabilities.streaming),
                    )
                except Exception as exc:
                    table.add_row(url, "[red]ERROR[/red]", "—", "—", str(exc))

    asyncio.run(_discover())
    console.print(table)


@cli.command("a2a-call")
@click.argument("skill_id")
@click.argument("json_payload")
@click.option(
    "--agent-urls",
    default=None,
    envvar="MESH_AGENT_URLS",
    help="Comma-separated agent base URLs",
)
def a2a_call(skill_id: str, json_payload: str, agent_urls: str | None) -> None:
    """Dispatch a single skill call and print the result.

    SKILL_ID is the skill to invoke (e.g. 'resolve_entities').
    JSON_PAYLOAD is the JSON input for that skill.
    """
    import asyncio
    import json as _json

    from mesh_a2a.client import MeshA2AClient

    _DEFAULT_URLS = [
        "http://localhost:8001",
        "http://localhost:8002",
        "http://localhost:8003",
        "http://localhost:8004",
    ]
    urls = [u.strip() for u in agent_urls.split(",")] if agent_urls else _DEFAULT_URLS

    try:
        payload = _json.loads(json_payload)
    except _json.JSONDecodeError as exc:
        console.print(f"[red]Invalid JSON: {exc}[/red]")
        raise SystemExit(1) from exc

    async def _call() -> Any:
        async with MeshA2AClient() as c:
            await c.discover(urls)
            return await c.call_skill(skill_id, payload)

    try:
        result = asyncio.run(_call())
        console.print_json(_json.dumps(result, indent=2))
    except Exception as exc:
        console.print(f"[red]Skill call failed: {exc}[/red]")
        raise SystemExit(1) from exc


@cli.group("schedule")
def schedule() -> None:
    """Inspect the APScheduler-managed pipeline + sweep jobs."""


@schedule.command("status")
def schedule_status() -> None:
    """Show next-run times and the latest run summary for each job.

    Asks each configured CronTrigger directly for its next-fire-time —
    the real scheduler runs in its own container. The latest-run summary
    comes from ``pipeline_runs`` so the user sees what actually happened,
    not just what was planned. The Checkpoint column reflects the latest
    LangGraph checkpoint state per job (in-flight / interrupted / finalized).
    """
    from mesh_a2a.checkpoint import read_run_states
    from mesh_scheduler import configured_cron_triggers

    now = datetime.now(UTC)
    triggers = configured_cron_triggers()
    next_runs: dict[str, datetime | None] = {
        job_id: trig.get_next_fire_time(None, now)
        for job_id, trig in triggers.items()
    }

    conn = _get_conn()
    try:
        recent_pipeline = list_pipeline_runs(conn, limit=1, run_type="pipeline")
        recent_sweep = list_pipeline_runs(conn, limit=1, run_type="skeptic_sweep")
    finally:
        conn.close()

    last_by_job = {
        "pipeline": recent_pipeline[0] if recent_pipeline else None,
        "skeptic_sweep": recent_sweep[0] if recent_sweep else None,
    }

    # Latest checkpoint state per run_type (read_run_states is newest-first;
    # empty when no Postgres checkpoint store is configured).
    threshold = int(os.environ.get("MESH_TASK_RESUME_THRESHOLD", "600"))
    latest_checkpoint: dict[str, Any] = {}
    for state in read_run_states():
        latest_checkpoint.setdefault(state.run_type, state)

    table = Table(title="Mesh schedule")
    table.add_column("Job", style="cyan")
    table.add_column("Next run", style="green")
    table.add_column("Last run", style="dim")
    table.add_column("Duration")
    table.add_column("Triggered by")
    table.add_column("Counts")
    table.add_column("Checkpoint")
    for job_id in ("pipeline", "skeptic_sweep"):
        next_run = next_runs.get(job_id)
        last = last_by_job.get(job_id)
        if last is None:
            last_str = "—"
            duration = "—"
            trig = "—"
            counts = "—"
        else:
            last_str = last.started_at.strftime("%Y-%m-%d %H:%M")
            if last.finished_at:
                secs = (last.finished_at - last.started_at).total_seconds()
                duration = f"{secs:.0f}s"
            else:
                duration = "running"
            trig = last.triggered_by
            if job_id == "pipeline":
                counts = (
                    f"claims +{last.claims_inserted} / "
                    f"beliefs +{last.beliefs_created}/~{last.beliefs_revised}"
                )
            else:
                counts = f"beliefs ~{last.beliefs_revised}"
        cp = latest_checkpoint.get(job_id)
        if cp is None:
            checkpoint = "—"
        elif cp.finalized:
            checkpoint = "finalized"
        elif cp.is_interrupted(threshold_seconds=threshold):
            checkpoint = "interrupted"
        else:
            checkpoint = "in flight"
        table.add_row(
            job_id,
            next_run.strftime("%Y-%m-%d %H:%M %Z") if next_run else "—",
            last_str,
            duration,
            trig,
            counts,
            checkpoint,
        )
    console.print(table)


def _print_relationship_detail(r: Any) -> None:
    from rich.panel import Panel

    lines = [
        f"[bold]ID:[/bold] {r.id}",
        f"[bold]From:[/bold] {r.from_entity_id}",
        f"[bold]To:[/bold] {r.to_entity_id}",
        f"[bold]Type:[/bold] {r.type}",
        f"[bold]Confidence:[/bold] {r.confidence:.2f}",
        f"[bold]Evidence:[/bold] {', '.join(r.evidence_claim_ids) or '—'}",
    ]
    console.print(Panel("\n".join(lines), title="Relationship"))


@cli.group("investigations")
def investigations() -> None:
    """Phase 7a investigation lifecycle inspection."""


_STATUS_CHOICES = click.Choice([s.value for s in InvestigationStatus])
_ORIGIN_CHOICES = click.Choice([o.value for o in InvestigationOrigin])


@investigations.command("list")
@click.option(
    "--status",
    "status_filter",
    type=_STATUS_CHOICES,
    default=None,
    help="Filter by status (open|in_progress|resolved|abandoned).",
)
@click.option(
    "--origin",
    "origin_filter",
    type=_ORIGIN_CHOICES,
    default=None,
    help="Filter by origin (curator|skeptic|discovery|manual).",
)
@click.option("--limit", default=50, type=int, show_default=True)
def investigations_list(
    status_filter: str | None, origin_filter: str | None, limit: int
) -> None:
    """List investigations with attached-claim + run-attempt counts."""
    conn = _get_conn()
    try:
        rows = list_investigations(
            conn,
            status=InvestigationStatus(status_filter) if status_filter else None,
            origin=InvestigationOrigin(origin_filter) if origin_filter else None,
            limit=limit,
        )
    finally:
        conn.close()

    if not rows:
        console.print("[dim]No investigations recorded.[/dim]")
        return

    table = Table(title="Investigations")
    table.add_column("ID", style="dim", max_width=8)
    table.add_column("Status", style="cyan")
    table.add_column("Origin", style="magenta")
    table.add_column("Target entity", style="dim", max_width=8)
    table.add_column("Belief", style="dim", max_width=8)
    table.add_column("Sources")
    table.add_column("Runs / Claims")
    table.add_column("Hypothesis", overflow="fold")
    for inv in rows:
        sources = ", ".join(inv.suggested_source_types) or "—"
        runs_claims = (
            f"{inv.pipeline_runs_attempted} run / "
            f"{len(inv.collected_claim_ids)} claims"
        )
        table.add_row(
            inv.id[:8],
            inv.status.value,
            inv.origin.value,
            (inv.target_entity_id or "—")[:8],
            (inv.opened_by_belief_id or "—")[:8],
            sources,
            runs_claims,
            inv.hypothesis or inv.question,
        )
    console.print(table)


@cli.command("discover")
@click.option("--field", default="ai-robotics", show_default=True, help="Field slug.")
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    default=False,
    help="Open discovery investigations + dispatch real search (default: dry-run).",
)
@click.option(
    "--report-path",
    default=None,
    help="Write the dry-run report to this file (text).",
)
def discover(field: str, apply_: bool, report_path: str | None) -> None:
    """Phase 22: autonomous discovery — analyze a field for gaps/trends and draft
    investigation hypotheses. Dry-run by default (lists what it WOULD open);
    --apply opens the investigations and dispatches real hypothesis-directed
    search through the running scout stack."""
    from mesh_db.fields import get_field_by_slug
    from mesh_llm import LLMProviderNotReadyError, make_routed_llm_client
    from mesh_models.field import DEFAULT_FIELD_ID
    from mesh_pipeline.discovery import _gap_limit, _max_new, plan_field_discovery
    from rich.panel import Panel

    if apply_:
        from mesh_pipeline.discovery import run_discovery

        result = asyncio.run(run_discovery(field))
        console.print(
            Panel(
                "\n".join(
                    [
                        f"[bold]Run:[/bold] {result.run_id}",
                        f"[bold]Gaps found:[/bold] {result.gaps_found}",
                        f"[bold]Hypotheses drafted:[/bold] {result.hypotheses_drafted}",
                        f"[bold]Investigations opened:[/bold] {result.investigations_opened}",
                        f"[bold]Fetches dispatched:[/bold] {result.fetches_dispatched}",
                        f"[bold]Claims inserted:[/bold] {result.claims_inserted}",
                    ]
                ),
                title=f"Discovery applied [{result.field_slug}]",
            )
        )
        return

    conn = _get_conn()
    try:
        field_row = get_field_by_slug(conn, field)
        field_id = field_row.id if field_row is not None else DEFAULT_FIELD_ID
        try:
            llm: Any = make_routed_llm_client(agent_name="discovery")
        except LLMProviderNotReadyError:
            llm = None
        gaps, proposals, built, _usage, _model = plan_field_discovery(
            conn, llm, field_id, gap_limit=_gap_limit(), max_new=_max_new()
        )
    finally:
        conn.close()

    gap_table = Table(title=f"Discovery gaps/trends [{field}] (dry-run)")
    gap_table.add_column("Kind", style="cyan")
    gap_table.add_column("Subject", style="bold")
    gap_table.add_column("Priority", justify="right")
    gap_table.add_column("Why", overflow="fold")
    for g in gaps:
        gap_table.add_row(g.kind.value, g.subject, f"{g.priority:.2f}", g.rationale)

    open_table = Table(title=f"Would open ({len(built)}, cap {_max_new()})")
    open_table.add_column("Origin", style="magenta")
    open_table.add_column("Sources")
    open_table.add_column("Hypothesis", overflow="fold")
    for inv in built:
        open_table.add_row(
            inv.origin.value,
            ", ".join(inv.suggested_source_types) or "—",
            inv.hypothesis or inv.question,
        )

    if gaps:
        console.print(gap_table)
    else:
        console.print("[dim]No gaps detected — field looks saturated.[/dim]")
    if built:
        console.print(open_table)
    elif gaps and llm is None:
        console.print(
            "[yellow]LLM unavailable — gaps detected but no hypotheses drafted "
            "(set ANTHROPIC_API_KEY or MESH_LLM_PROVIDER=ollama).[/yellow]"
        )
    console.print(
        f"[dim]{len(gaps)} gaps, {len(proposals)} hypotheses, "
        f"{len(built)} investigations would open. Re-run with --apply to act.[/dim]"
    )

    if report_path:
        lines = [f"Discovery dry-run report — field={field}", ""]
        lines.append(f"Gaps ({len(gaps)}):")
        for g in gaps:
            lines.append(f"  [{g.kind.value}] {g.subject} (p={g.priority:.2f}) — {g.rationale}")
        lines.append("")
        lines.append(f"Would open ({len(built)}):")
        for inv in built:
            srcs = ", ".join(inv.suggested_source_types) or "—"
            lines.append(f"  ({srcs}) {inv.hypothesis or inv.question}")
        with open(report_path, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        console.print(f"[green]Report written to {report_path}[/green]")


@cli.group("heuristics")
def heuristics() -> None:
    """Phase 16 procedural-memory inspection."""


@heuristics.command("list")
@click.option("--agent", default=None, help="Filter by agent (e.g. claim_extractor).")
@click.option("--skill", default=None, help="Filter by skill (e.g. extract_claims).")
@click.option(
    "--include-expired/--active-only",
    default=False,
    show_default=True,
    help="Include heuristics past their TTL (default: only unexpired).",
)
@click.option("--limit", default=50, type=int, show_default=True)
def heuristics_list(
    agent: str | None, skill: str | None, include_expired: bool, limit: int
) -> None:
    """List learned heuristics with agent, skill, scope, confidence, and TTL."""
    conn = _get_conn()
    try:
        rows = list_heuristics(
            conn,
            agent=agent,
            skill=skill,
            include_expired=include_expired,
            limit=limit,
        )
    finally:
        conn.close()

    if not rows:
        console.print("[dim]No heuristics recorded.[/dim]")
        return

    table = Table(title="Agent heuristics")
    table.add_column("ID", style="dim", max_width=8)
    table.add_column("Agent", style="cyan")
    table.add_column("Skill")
    table.add_column("Scope", style="dim")
    table.add_column("Conf", justify="right")
    table.add_column("Active")
    table.add_column("Expires", style="dim")
    table.add_column("Heuristic", overflow="fold")
    for h in rows:
        scope_bits = [b for b in (h.source, h.entity_id) if b]
        scope = ", ".join(scope_bits) or "—"
        table.add_row(
            h.id[:8],
            h.agent,
            h.skill,
            scope,
            f"{h.confidence:.2f}",
            "yes" if h.is_currently_active else "no",
            h.expires_at.strftime("%Y-%m-%d"),
            h.heuristic,
        )
    console.print(table)


@cli.command("backfill-entity-embeddings")
@click.option("--batch-size", default=256, type=int, show_default=True)
@click.option(
    "--all",
    "embed_all",
    is_flag=True,
    help="Re-embed every entity (default: only those missing an embedding).",
)
def backfill_entity_embeddings(batch_size: int, embed_all: bool) -> None:
    """Populate entities.name_embedding for entity-resolution blocking (Phase 13a).

    Batches embedding calls and writes via the writer role. Re-runnable: by
    default it skips entities that already have an embedding.
    """
    from mesh_db.entities import set_entity_embedding
    from mesh_llm import entity_embed_text, make_embedder

    embedder = make_embedder()
    conn = get_connection()  # writer
    try:
        where = "" if embed_all else " WHERE name_embedding IS NULL"
        rows = conn.execute(
            f"SELECT id, canonical_name, type FROM entities{where} ORDER BY created_at"
        ).fetchall()
        total = len(rows)
        if total == 0:
            console.print("[green]All entities already have embeddings.[/green]")
            return
        console.print(f"Embedding [cyan]{total}[/cyan] entities (batch={batch_size})…")
        done = 0
        for start in range(0, total, batch_size):
            chunk = rows[start : start + batch_size]
            texts = [entity_embed_text(str(r[1]), str(r[2])) for r in chunk]
            vectors = embedder.embed(texts)
            for (entity_id, _name, _type), vec in zip(chunk, vectors, strict=True):
                set_entity_embedding(conn, str(entity_id), vec)
            done += len(chunk)
            console.print(f"  …{done}/{total}")
        console.print(f"[green]Backfilled {done} entity embeddings.[/green]")
    finally:
        conn.close()


@cli.command("reconcile-entities")
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    help="Perform merges (default: dry-run — compute + report only).",
)
@click.option(
    "--report",
    "report_path",
    default="docs/entity-resolution-reconciliation.md",
    show_default=True,
)
@click.option("--k", default=10, type=int, show_default=True, help="Blocking neighbours.")
@click.option(
    "--field",
    default="ai-robotics",
    show_default=True,
    help="Field slug to scope reconciliation to (resolution never crosses fields).",
)
def reconcile_entities_cmd(
    apply_changes: bool, report_path: str, k: int, field: str
) -> None:
    """One-time reconciliation of accumulated duplicate entities (Phase 13c).

    Blocks → matches → merges across one field's entity table. Middle-band
    adjudications route through the Anthropic Batch API when available. Writes a
    report (before/after counts, sample of merges) for false-merge review.
    Idempotent — re-running finds little to do.
    """
    from pathlib import Path

    from mesh_agents.reconcile import reconcile_entities, render_report_markdown
    from mesh_llm import make_embedder, make_llm_client

    embedder = make_embedder()
    llm: Any | None
    try:
        llm = make_llm_client(agent_name="entity_resolution")
        llm.health_check()
    except Exception as exc:  # provider not ready → high-band auto-merges only
        console.print(f"[yellow]LLM unavailable ({exc}); adjudicating none.[/yellow]")
        llm = None

    mode = "APPLY" if apply_changes else "dry-run"
    console.print(f"Reconciling entities ([cyan]{mode}[/cyan])…")
    conn = get_connection()  # writer
    try:
        report = reconcile_entities(
            conn, embedder, llm, k=k, dry_run=not apply_changes, field_id=field
        )
    finally:
        conn.close()

    console.print(
        f"[green]before={report.entities_before} after={report.entities_after} "
        f"merges={report.merges} auto={report.auto_merges} "
        f"adjudicated={report.adjudications} embedded={report.embedded_now}[/green]"
    )
    out = Path(report_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report_markdown(report))
    console.print(f"Report written to [cyan]{out}[/cyan]")
    if not apply_changes and report.merges:
        console.print(
            "[yellow]Dry run: re-run with --apply to perform these merges.[/yellow]"
        )


@cli.command("consolidate-beliefs")
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    help="Perform merges + decay/archival (default: dry-run — compute + report).",
)
@click.option(
    "--report-path",
    "report_path",
    default="docs/belief-consolidation-report.md",
    show_default=True,
)
@click.option("--k", default=10, type=int, show_default=True, help="Blocking neighbours.")
@click.option(
    "--field",
    default="ai-robotics",
    show_default=True,
    help="Field slug to scope consolidation to (never crosses fields).",
)
@click.option(
    "--no-decay",
    "no_decay",
    is_flag=True,
    help="Skip the staleness decay + archival pass (merge only).",
)
def consolidate_beliefs_cmd(
    apply_changes: bool, report_path: str, k: int, field: str, no_decay: bool
) -> None:
    """One-time belief consolidation over one field's held corpus (Phase 19).

    Backfills statement_embedding for any held belief missing one, then
    blocks → matches → merges semantic duplicates (middle band via the Anthropic
    Batch API when available) and ages stale beliefs (decay + archival). Writes a
    report (before/after counts, sample of merges) for false-merge review.
    Read-only by default — pass --apply to write. Idempotent.
    """
    from pathlib import Path

    from mesh_agents.belief_reconcile import reconcile_beliefs, render_report_markdown
    from mesh_db.fields import get_field_by_slug
    from mesh_llm import make_embedder, make_llm_client

    embedder = make_embedder()
    llm: Any | None
    try:
        llm = make_llm_client(agent_name="belief_consolidator")
        llm.health_check()
    except Exception as exc:  # provider not ready → high-band auto-merges only
        console.print(f"[yellow]LLM unavailable ({exc}); adjudicating none.[/yellow]")
        llm = None

    mode = "APPLY" if apply_changes else "dry-run"
    console.print(f"Consolidating beliefs ([cyan]{mode}[/cyan])…")
    conn = get_connection()  # writer
    try:
        fld = get_field_by_slug(conn, field)
        field_id = fld.id if fld is not None else field
        report = reconcile_beliefs(
            conn, embedder, llm, k=k, dry_run=not apply_changes,
            decay=not no_decay, field_id=field_id,
        )
    finally:
        conn.close()

    console.print(
        f"[green]held before={report.beliefs_held_before} "
        f"after={report.beliefs_held_after} merges={report.merges} "
        f"auto={report.auto_merges} adjudicated={report.adjudications} "
        f"decayed={report.decayed} archived={report.archived} "
        f"embedded={report.embedded_now}[/green]"
    )
    out = Path(report_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_report_markdown(report))
    console.print(f"Report written to [cyan]{out}[/cyan]")
    if not apply_changes and report.merges:
        console.print(
            "[yellow]Dry run: re-run with --apply to perform these merges.[/yellow]"
        )


@cli.group("beliefs")
def beliefs() -> None:
    """Phase 19 belief-consolidation inspection."""


@beliefs.command("duplicates")
@click.option(
    "--field",
    default="ai-robotics",
    show_default=True,
    help="Field slug to scope to (consolidation never crosses fields).",
)
@click.option("--k", default=10, type=int, show_default=True, help="Blocking neighbours.")
@click.option("--limit", default=50, type=int, show_default=True)
def beliefs_duplicates(field: str, k: int, limit: int) -> None:
    """List candidate duplicate belief pairs above the low band (read-only).

    Embeds + blocks each held belief and shows pairs whose cosine similarity
    clears the reject floor, with their band (merge / adjudicate), so pending
    consolidation can be eyeballed without running the sweep.
    """
    from mesh_agents.belief_consolidation import BeliefMergeConfig, band
    from mesh_agents.belief_reconcile import ensure_belief_embeddings
    from mesh_db.beliefs import belief_family, find_candidate_duplicate_beliefs
    from mesh_db.fields import get_field_by_slug
    from mesh_llm import belief_embed_text, make_embedder

    cfg = BeliefMergeConfig.from_env()
    embedder = make_embedder()
    conn = get_connection()  # writer (embedding backfill may write)
    try:
        fld = get_field_by_slug(conn, field)
        field_id = fld.id if fld is not None else field
        ensure_belief_embeddings(conn, embedder, field_id)
        held = list_beliefs(conn, currently_held=True, limit=100000, field_id=field_id)
        seen: set[frozenset[str]] = set()
        rows: list[tuple[str, str, str, str, float, str]] = []
        for b in held:
            vec = embedder.embed([belief_embed_text(b.topic, b.statement)])[0]
            family = belief_family(b.topic)
            for nb_id, nb_topic, _stmt, distance in find_candidate_duplicate_beliefs(
                conn, vec, exclude_id=b.id, k=k, field_id=field_id, family=family
            ):
                pair = frozenset({b.id, nb_id})
                if len(pair) < 2 or pair in seen:
                    continue
                similarity = 1.0 - distance
                decision = band(similarity, cfg)
                if decision == "reject":
                    continue
                seen.add(pair)
                rows.append((b.id, b.topic, nb_id, nb_topic, similarity, decision))
    finally:
        conn.close()

    rows.sort(key=lambda r: r[4], reverse=True)
    rows = rows[:limit]
    if not rows:
        console.print("[dim]No candidate duplicate belief pairs above the low band.[/dim]")
        return

    table = Table(title=f"Candidate duplicate beliefs ({field})")
    table.add_column("A", style="dim", max_width=8)
    table.add_column("Topic A", overflow="fold")
    table.add_column("B", style="dim", max_width=8)
    table.add_column("Topic B", overflow="fold")
    table.add_column("Cosine", justify="right")
    table.add_column("Band", style="cyan")
    for a_id, a_topic, b_id, b_topic, sim, dband in rows:
        table.add_row(
            a_id[:8], a_topic, b_id[:8], b_topic, f"{sim:.3f}", dband
        )
    console.print(table)

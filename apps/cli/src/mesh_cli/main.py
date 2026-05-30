from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any

import click
from mesh_db.beliefs import create_belief, get_belief_by_id, list_beliefs, update_belief
from mesh_db.claims import create_claim, get_claim_by_id, list_claims
from mesh_db.connection import get_connection
from mesh_db.entities import create_entity, get_entity_by_id, list_entities
from mesh_db.investigations import get_investigation_by_id, list_investigations
from mesh_db.llm_usage import aggregate_usage_by_skill
from mesh_db.migrations import apply_migrations
from mesh_db.pipeline_runs import list_pipeline_runs
from mesh_db.relationships import get_relationship_by_id
from mesh_db.revisions import create_revision, get_revision_by_id, list_revisions
from mesh_db.sources import create_source, get_source_by_id, list_sources
from mesh_models.belief import Belief
from mesh_models.claim import Claim, ClaimStatus
from mesh_models.entity import Entity, EntityType
from mesh_models.investigation import Investigation, InvestigationStatus
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
    """Create the database and apply all migrations."""
    conn = _get_conn()
    apply_migrations(conn)
    conn.close()
    console.print("[green]Database initialized.[/green]")


@cli.command("init-pg-db")
def init_pg_db() -> None:
    """Stand up the Postgres knowledge schema + roles (Phase 12).

    Uses MESH_PG_URL / LANGGRAPH_POSTGRES_URL. Run as a superuser/DB owner so
    CREATE EXTENSION + CREATE ROLE succeed. Idempotent.
    """
    from mesh_db.pg_migrations import init_pg

    applied = init_pg()
    if applied:
        console.print(
            f"[green]Postgres knowledge schema initialized.[/green] "
            f"Applied: {', '.join(applied)}"
        )
    else:
        console.print("[green]Postgres knowledge schema already up to date.[/green]")


@cli.command("migrate-duckdb-to-pg")
def migrate_duckdb_to_pg() -> None:
    """One-time DuckDB -> Postgres knowledge data migration (Phase 12c).

    Reads MESH_DB_PATH (DuckDB) and writes into the knowledge schema at
    MESH_PG_URL / LANGGRAPH_POSTGRES_URL. Idempotent (truncate-and-reload).
    Run init-pg-db first.
    """
    from mesh_db.duckdb_to_pg import run

    run()


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


@investigations.command("list")
@click.option(
    "--status",
    "status_filter",
    type=_STATUS_CHOICES,
    default=None,
    help="Filter by status (open|in_progress|resolved|abandoned).",
)
@click.option("--limit", default=50, type=int, show_default=True)
def investigations_list(status_filter: str | None, limit: int) -> None:
    """List investigations with attached-claim + run-attempt counts."""
    conn = _get_conn()
    try:
        if status_filter:
            rows = list_investigations(
                conn, status=InvestigationStatus(status_filter), limit=limit
            )
        else:
            rows = list_investigations(conn, limit=limit)
    finally:
        conn.close()

    if not rows:
        console.print("[dim]No investigations recorded.[/dim]")
        return

    table = Table(title="Investigations")
    table.add_column("ID", style="dim", max_width=8)
    table.add_column("Status", style="cyan")
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
            (inv.target_entity_id or "—")[:8],
            (inv.opened_by_belief_id or "—")[:8],
            sources,
            runs_claims,
            inv.hypothesis or inv.question,
        )
    console.print(table)

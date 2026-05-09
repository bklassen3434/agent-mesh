from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import click
from mesh_db.beliefs import create_belief, get_belief_by_id, list_beliefs, update_belief
from mesh_db.claims import create_claim, get_claim_by_id, list_claims
from mesh_db.connection import get_connection
from mesh_db.entities import create_entity, get_entity_by_id, list_entities
from mesh_db.investigations import get_investigation_by_id
from mesh_db.migrations import apply_migrations
from mesh_db.pipeline_runs import list_pipeline_runs
from mesh_db.relationships import get_relationship_by_id
from mesh_db.revisions import create_revision, get_revision_by_id, list_revisions
from mesh_db.sources import create_source, get_source_by_id, list_sources
from mesh_models.belief import Belief
from mesh_models.claim import Claim, ClaimStatus
from mesh_models.entity import Entity, EntityType
from mesh_models.investigation import Investigation
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

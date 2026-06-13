from __future__ import annotations

import asyncio
import os

import click
import structlog

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ]
)


@click.command()
@click.option(
    "--categories",
    default=os.environ.get("MESH_PIPELINE_CATEGORIES"),
    show_default=True,
    help=(
        "Comma-separated arxiv categories — overrides the field's arxiv "
        "connector config for this run. Unset: use the per-field connector config."
    ),
)
@click.option(
    "--max-papers",
    default=int(os.environ.get("MESH_PIPELINE_MAX_PAPERS", "20")),
    type=int,
    show_default=True,
    help="Maximum number of papers to fetch",
)
@click.option(
    "--since",
    default=None,
    help="Fetch papers since this date/duration (e.g. 24h, 7d, 2024-01-01)",
)
@click.option(
    "--db-path",
    default=None,
    envvar="MESH_DB_PATH",
    help="(deprecated; ignored — the store is Postgres)",
)
@click.option(
    "--field",
    default=os.environ.get("MESH_PIPELINE_FIELD", "ai-robotics"),
    show_default=True,
    help="Field slug to scope this run to (A2A coordinator only)",
)
@click.option(
    "--a2a",
    "use_a2a",
    is_flag=True,
    default=os.environ.get("MESH_USE_A2A", "").lower() in ("1", "true", "yes"),
    help="Use A2A coordinator instead of in-process orchestrator",
)
def main(
    categories: str | None,
    max_papers: int,
    since: str | None,
    db_path: str | None,
    field: str,
    use_a2a: bool,
) -> None:
    """Run the Agent Mesh ingestion pipeline."""
    cats = (
        [c.strip() for c in categories.split(",") if c.strip()]
        if categories
        else None
    )

    if use_a2a:
        from mesh_pipeline.coordinator import parse_since, run_pipeline

        since_dt = parse_since(since)
        result = asyncio.run(
            run_pipeline(
                categories=cats,
                max_papers=max_papers,
                since=since_dt,
                db_path=db_path,
                field=field,
            )
        )
    else:
        from mesh_pipeline.orchestrator import (  # type: ignore[assignment]
            parse_since,
            run_pipeline,
        )

        since_dt = parse_since(since)
        # The legacy in-process orchestrator predates per-field connectors; it
        # still takes a concrete category list.
        result = asyncio.run(
            run_pipeline(
                categories=cats or ["cs.AI", "cs.RO", "cs.LG"],
                max_papers=max_papers,
                since=since_dt,
                db_path=db_path,
            )
        )

    click.echo(f"\nPipeline run {result.run_id}")
    click.echo(f"  Papers scouted:    {result.papers_scouted}")
    # items_skipped (dedup-before-extraction) only exists on the A2A
    # coordinator's result, not the in-process orchestrator's.
    items_skipped = getattr(result, "items_skipped", None)
    if items_skipped is not None:
        click.echo(f"  Items skipped:     {items_skipped}")
    click.echo(f"  Sources inserted:  {result.sources_inserted}")
    click.echo(f"  Claims inserted:   {result.claims_inserted}")
    click.echo(f"  Entities created:  {result.entities_created}")
    click.echo(f"  Beliefs created:   {result.beliefs_created}")
    click.echo(f"  Beliefs revised:   {result.beliefs_revised}")
    click.echo(f"  Avg LLM latency:   {result.avg_extraction_latency_ms}ms")
    if result.errors:
        click.echo(f"  Errors:            {len(result.errors)}")
        for err in result.errors:
            click.echo(f"    [{err['paper_id']}] {err['error_type']}: {err['error_message']}")

"""GET /status — operational status page.

Server-rendered HTML, no JS. Meta-refresh every 60s. Resists every pull
toward becoming a real dashboard — the wiki has its own visualization
story, this is for "is the mesh healthy?" at a glance.
"""
from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from mesh_a2a.checkpoint import RunCheckpointState, read_run_states
from mesh_db.beliefs import count_beliefs
from mesh_db.claims import count_claims
from mesh_db.connection import MeshConnection
from mesh_db.pipeline_runs import PipelineRun, list_pipeline_runs
from mesh_db.sources import count_sources

from mesh_api.deps import ConnDep

router = APIRouter(tags=["status"])


# ── data gathering ─────────────────────────────────────────────────────────


def _last_run(conn: MeshConnection, run_type: str) -> PipelineRun | None:
    rows = list_pipeline_runs(conn, limit=1, run_type=run_type)
    return rows[0] if rows else None


def _duration_seconds(run: PipelineRun) -> int | None:
    if run.finished_at is None:
        return None
    return int((run.finished_at - run.started_at).total_seconds())


def _interrupted_threshold() -> int:
    """Seconds a non-finalized run's latest checkpoint can age before /status
    flags it interrupted. Reuses the old orphan-sweep knob."""
    return int(os.environ.get("MESH_TASK_RESUME_THRESHOLD", "600"))


def _ago(when: datetime) -> str:
    delta = datetime.now(UTC) - when
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _next_runs() -> dict[str, datetime | None]:
    try:
        from mesh_scheduler import configured_cron_triggers
    except ImportError:
        return {"ingest": None, "skeptic": None}
    now = datetime.now(UTC)
    out: dict[str, datetime | None] = {}
    for job_id, trig in configured_cron_triggers().items():
        try:
            out[job_id] = trig.get_next_fire_time(None, now)
        except Exception:
            out[job_id] = None
    return out


def _sources_by_type(conn: MeshConnection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT type, COUNT(*) FROM sources GROUP BY type ORDER BY type"
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def _langfuse_24h_trace_count() -> int | None:
    """Best-effort GET to Langfuse public API. None on any failure.

    Uses HTTP Basic with the configured keys (Langfuse v2 convention).
    Short timeout so a flaky/missing Langfuse never delays the page.
    """
    pub = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    sec = os.environ.get("LANGFUSE_SECRET_KEY", "")
    host = os.environ.get("LANGFUSE_HOST", "")
    if not (pub and sec and host):
        return None
    creds = base64.b64encode(f"{pub}:{sec}".encode()).decode()
    since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    try:
        with httpx.Client(timeout=2.0) as http:
            resp = http.get(
                f"{host.rstrip('/')}/api/public/traces",
                params={"fromTimestamp": since, "limit": 1},
                headers={"Authorization": f"Basic {creds}"},
            )
        if resp.status_code != 200:
            return None
        payload: dict[str, Any] = resp.json()
        meta = payload.get("meta") or {}
        total = meta.get("totalItems")
        return int(total) if total is not None else None
    except Exception:
        return None


# ── rendering ──────────────────────────────────────────────────────────────


_STYLES = """
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         max-width: 880px; margin: 2rem auto; padding: 0 1.5rem; color: #111;
         line-height: 1.45; background: #fafafa; }
  h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
  .sub { color: #666; font-size: .85rem; margin-bottom: 1.5rem; }
  section { background: #fff; border: 1px solid #e3e3e3; border-radius: 6px;
            padding: 1rem 1.25rem; margin-bottom: 1rem; }
  section h2 { font-size: 1rem; margin: 0 0 .5rem; }
  table { width: 100%; border-collapse: collapse; font-size: .9rem; }
  td { padding: .35rem .5rem; border-bottom: 1px solid #f0f0f0; vertical-align: top; }
  td.k { color: #555; white-space: nowrap; width: 11rem; }
  td.v { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .ok { color: #0a7d2b; }
  .warn { color: #b35900; }
  .bad { color: #b00020; }
  .muted { color: #888; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: .25rem 1.5rem; }
  .grid .k { width: auto; }
  ul { margin: .25rem 0 0 .25rem; padding-left: 1rem; }
  li { font-size: .85rem; margin: .1rem 0; font-family: ui-monospace, monospace; }
  a { color: #1a4cb8; }
</style>
"""


def _row(k: str, v: str, *, cls: str = "") -> str:
    cls_attr = f' class="{cls}"' if cls else ""
    return f'<tr><td class="k">{k}</td><td class="v"{cls_attr}>{v}</td></tr>'


def _section_runs(conn: MeshConnection) -> str:
    pipeline = _last_run(conn, "ingest")
    sweep = _last_run(conn, "skeptic")
    nexts = _next_runs()
    rows: list[str] = []
    for label, run, next_run_key in (
        ("Ingest", pipeline, "ingest"),
        ("Skeptic sweep", sweep, "skeptic"),
    ):
        if run is None:
            rows.append(_row(f"{label} — last run", "never", cls="muted"))
        else:
            dur = _duration_seconds(run)
            dur_str = f"{dur}s" if dur is not None else "running"
            errs = len(run.errors)
            err_class = "bad" if errs else "ok"
            rows.append(
                _row(
                    f"{label} — last run",
                    f"{run.started_at.strftime('%Y-%m-%d %H:%M')} "
                    f"({_ago(run.started_at)}) · dur {dur_str} · "
                    f"{run.triggered_by} · "
                    f"<span class='{err_class}'>{errs} errors</span>",
                )
            )
            if label == "Ingest":
                rows.append(
                    _row(
                        "&nbsp;&nbsp;&nbsp;deltas",
                        f"+{run.claims_inserted} claims, "
                        f"+{run.beliefs_created} / ~{run.beliefs_revised} beliefs",
                    )
                )
            else:
                rows.append(
                    _row(
                        "&nbsp;&nbsp;&nbsp;deltas",
                        f"~{run.beliefs_revised} beliefs revised",
                    )
                )
        nxt = nexts.get(next_run_key)
        rows.append(
            _row(
                f"{label} — next run",
                nxt.strftime("%Y-%m-%d %H:%M %Z") if nxt else "—",
                cls="" if nxt else "muted",
            )
        )
    return f"<section><h2>Runs</h2><table>{''.join(rows)}</table></section>"


def _section_counts(
    conn: MeshConnection, run_states: list[RunCheckpointState]
) -> str:
    n_claims = count_claims(conn)
    n_beliefs_held = count_beliefs(conn, currently_held=True)
    n_beliefs_total = count_beliefs(conn)
    n_sources = count_sources(conn)
    by_type = _sources_by_type(conn)
    rows = [
        _row("Claims", str(n_claims)),
        _row(
            "Beliefs",
            f"{n_beliefs_held} held / {n_beliefs_total} total",
        ),
        _row("Sources", str(n_sources)),
    ]
    if by_type:
        type_line = ", ".join(f"{k} {v}" for k, v in by_type.items())
        rows.append(_row("&nbsp;&nbsp;&nbsp;by type", type_line))

    threshold = _interrupted_threshold()
    interrupted = sum(
        1 for s in run_states if s.is_interrupted(threshold_seconds=threshold)
    )
    in_flight = sum(1 for s in run_states if not s.finalized) - interrupted
    rows.append(_row("Runs — checkpointed", str(len(run_states))))
    rows.append(_row("Runs — in flight", str(max(in_flight, 0))))
    rows.append(
        _row("Runs — interrupted", str(interrupted), cls="bad" if interrupted else "")
    )
    return f"<section><h2>Counts</h2><table>{''.join(rows)}</table></section>"


def _section_failures(run_states: list[RunCheckpointState]) -> str:
    """Errors surfaced from each run's latest LangGraph checkpoint state."""
    items: list[str] = []
    for state in run_states:
        for err in state.errors:
            skill = str(err.get("skill_id") or "?")
            msg = str(err.get("error_message") or err.get("error_type") or "—")
            items.append(
                f"<li><span class='bad'>{skill}</span> · "
                f"{msg} · "
                f"<span class='muted'>{state.run_type} {state.run_id[:8]}</span></li>"
            )
            if len(items) >= 10:
                break
        if len(items) >= 10:
            break
    if not items:
        return (
            "<section><h2>Recent run errors</h2>"
            "<p class='muted'>No errors recorded.</p></section>"
        )
    return f"<section><h2>Recent run errors</h2><ul>{''.join(items)}</ul></section>"


def _section_langfuse() -> str:
    host = os.environ.get("LANGFUSE_HOST")
    if not host:
        return (
            "<section><h2>Tracing</h2>"
            "<p class='muted'>Langfuse not configured.</p></section>"
        )
    count = _langfuse_24h_trace_count()
    if count is None:
        body = (
            "<p class='warn'>Langfuse configured but trace count "
            "unavailable (check connectivity / credentials).</p>"
        )
    else:
        body = (
            f"<p><span class='ok'>{count}</span> trace(s) in the last 24h. "
            f"<a href='{host}'>open Langfuse →</a></p>"
        )
    return f"<section><h2>Tracing</h2>{body}</section>"


@router.get(
    "/status",
    response_class=HTMLResponse,
    summary="Operational status page",
    description=(
        "Server-rendered HTML with meta-refresh every 60s. Shows last + next "
        "runs, total row counts, LangGraph checkpoint run state (in-flight / "
        "interrupted), recent run errors, and the Langfuse 24h trace count when "
        "configured."
    ),
)
def status_page(conn: ConnDep) -> HTMLResponse:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S %Z")
    # One read of the checkpoint store per request, shared across panels.
    run_states = read_run_states()
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Mesh status</title>
  <meta http-equiv="refresh" content="60">
  {_STYLES}
</head>
<body>
  <h1>Mesh status</h1>
  <p class="sub">As of {now} · auto-refresh 60s</p>
  {_section_runs(conn)}
  {_section_counts(conn, run_states)}
  {_section_failures(run_states)}
  {_section_langfuse()}
</body>
</html>"""
    return HTMLResponse(content=html)

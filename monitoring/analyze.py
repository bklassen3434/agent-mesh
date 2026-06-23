#!/usr/bin/env python3
"""monitoring/analyze.py — render trends from the Pi's metrics snapshots.

Reads a JSONL file (one snapshot per line, produced by snapshot.sql) and prints
a trend table for the metrics that actually drive tuning decisions, the latest
full snapshot, and a set of automatic health flags. Stdlib only.

Usage: python3 monitoring/analyze.py [path/to/snapshots.jsonl] [--last N]
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone


def load(path: str) -> list[dict]:
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def g(d: dict, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, {})
    return d if d != {} else default


def fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".") if v < 1 else f"{v:.2f}"
    return str(v)


def age_hours(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600
    except ValueError:
        return None


# The columns worth watching over time, each (label, accessor).
COLS = [
    ("time(UTC)", lambda r: g(r, "ts", default="?")[5:16]),
    ("claims", lambda r: g(r, "kb", "claims_total", default=0)),
    ("ents", lambda r: g(r, "kb", "entities", default=0)),
    ("bel_held", lambda r: g(r, "kb", "beliefs_held", default=0)),
    ("conf_avg", lambda r: g(r, "quality", "belief_conf_avg")),
    ("inv_24h", lambda r: g(r, "controller", "invocations_24h", default=0)),
    ("err_24h", lambda r: g(r, "controller", "errors_24h", default=0)),
    ("invs", lambda r: g(r, "kb", "investigations_total", default=0)),
    ("cost_24h", lambda r: g(r, "cost", "cost_usd_24h", default=0)),
    ("cost_tot", lambda r: g(r, "cost", "cost_usd_total", default=0)),
]


def trend_table(rows: list[dict]) -> None:
    headers = [c[0] for c in COLS]
    widths = [len(h) for h in headers]
    cells = []
    for r in rows:
        row = [fmt(acc(r)) for _, acc in COLS]
        cells.append(row)
        widths = [max(w, len(c)) for w, c in zip(widths, row)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("  ".join("-" * w for w in widths))
    for row in cells:
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)))


def flags(latest: dict) -> list[str]:
    out = []
    inv_age = age_hours(g(latest, "controller", "last_invocation_at"))
    if inv_age is None:
        out.append("⚠ controller has NEVER run (no agent_invocations)")
    elif inv_age > 8:
        out.append(f"⚠ controller idle {inv_age:.1f}h — scheduler may not be firing")
    errs = g(latest, "controller", "errors_24h", default=0) or 0
    if errs:
        out.append(f"⚠ {errs} skill errors in last 24h: {g(latest, 'controller', 'errors_by_type_24h', default={})}")
    claims = g(latest, "kb", "claims_total", default=0) or 0
    held = g(latest, "kb", "beliefs_held", default=0) or 0
    if claims >= 20 and held == 0:
        out.append(f"⚠ {claims} claims but 0 held beliefs — synthesis not producing beliefs")
    conf = g(latest, "quality", "belief_conf_avg")
    if conf is not None and conf < 0.4:
        out.append(f"⚠ mean belief confidence low ({conf}) — check confidence weights / evidence depth")
    if not out:
        out.append("✓ no flags")
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0] if args else ".context/snapshots.jsonl"
    last = 30
    if "--last" in sys.argv:
        last = int(sys.argv[sys.argv.index("--last") + 1])

    rows = load(path)
    if not rows:
        print(f"no snapshots in {path}")
        return 1

    print(f"=== {len(rows)} snapshots in {path} (showing last {min(last, len(rows))}) ===\n")
    trend_table(rows[-last:])

    latest = rows[-1]
    print("\n=== latest snapshot ===")
    print(json.dumps(latest, indent=2))

    print("\n=== flags ===")
    for f in flags(latest):
        print(" ", f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

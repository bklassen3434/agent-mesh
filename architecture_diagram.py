"""Generate the Agent Mesh A2A + LangGraph architecture diagram as SVG."""
from __future__ import annotations

SVG_W = 1400
SVG_H = 1100

# Color palette
C_BG       = "#0f1117"
C_PANEL    = "#1a1d27"
C_BORDER   = "#2e3347"
C_BLUE     = "#4c9be8"
C_TEAL     = "#3dd6c0"
C_PURPLE   = "#9b7fe8"
C_ORANGE   = "#e8974c"
C_GREEN    = "#5ccc8a"
C_RED      = "#e85c5c"
C_YELLOW   = "#e8d44c"
C_GREY     = "#6b7280"
C_TEXT     = "#e2e8f0"
C_SUBTEXT  = "#94a3b8"
C_WHITE    = "#ffffff"
C_POSTGRES = "#336791"
C_DUCKDB   = "#f6a623"


def rect(x, y, w, h, fill, rx=8, stroke=None, stroke_w=1.5, opacity=1.0):
    s = f'stroke="{stroke}" stroke-width="{stroke_w}"' if stroke else 'stroke="none"'
    op = f'opacity="{opacity}"' if opacity != 1.0 else ""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" {s} {op}/>\n'


def text(x, y, content, size=12, fill=C_TEXT, anchor="middle", weight="normal", family="monospace"):
    return f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" font-weight="{weight}" font-family="{family}">{content}</text>\n'


def line(x1, y1, x2, y2, stroke=C_BORDER, w=1.5, dash=""):
    d = f'stroke-dasharray="{dash}"' if dash else ""
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" stroke-width="{w}" {d}/>\n'


def arrow(x1, y1, x2, y2, stroke=C_BLUE, w=1.5, dash="", marker="url(#arr)"):
    d = f'stroke-dasharray="{dash}"' if dash else ""
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" stroke-width="{w}" {d} marker-end="{marker}"/>\n'


def node_box(x, y, w, h, label, sublabel="", fill=C_PANEL, stroke=C_BLUE, icon=""):
    out = rect(x, y, w, h, fill, rx=6, stroke=stroke)
    lx = x + w // 2
    if sublabel:
        out += text(lx, y + h // 2 - 5, f"{icon}{label}", size=11, fill=C_TEXT, weight="bold")
        out += text(lx, y + h // 2 + 11, sublabel, size=9, fill=C_SUBTEXT)
    else:
        out += text(lx, y + h // 2 + 4, f"{icon}{label}", size=11, fill=C_TEXT, weight="bold")
    return out


def section_label(x, y, label, color=C_SUBTEXT):
    return text(x, y, label, size=10, fill=color, anchor="start", weight="bold")


def build_svg() -> str:
    parts: list[str] = []

    # ── defs ─────────────────────────────────────────────────────────────────
    parts.append(f'''<svg xmlns="http://www.w3.org/2000/svg" width="{SVG_W}" height="{SVG_H}" viewBox="0 0 {SVG_W} {SVG_H}">
<defs>
  <marker id="arr" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
    <path d="M0,0 L0,6 L8,3 z" fill="{C_BLUE}"/>
  </marker>
  <marker id="arr_teal" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
    <path d="M0,0 L0,6 L8,3 z" fill="{C_TEAL}"/>
  </marker>
  <marker id="arr_purple" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
    <path d="M0,0 L0,6 L8,3 z" fill="{C_PURPLE}"/>
  </marker>
  <marker id="arr_orange" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
    <path d="M0,0 L0,6 L8,3 z" fill="{C_ORANGE}"/>
  </marker>
  <marker id="arr_grey" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
    <path d="M0,0 L0,6 L8,3 z" fill="{C_GREY}"/>
  </marker>
  <marker id="arr_green" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
    <path d="M0,0 L0,6 L8,3 z" fill="{C_GREEN}"/>
  </marker>
</defs>
''')

    # ── background ────────────────────────────────────────────────────────────
    parts.append(rect(0, 0, SVG_W, SVG_H, C_BG, rx=0))

    # ── title ─────────────────────────────────────────────────────────────────
    parts.append(text(SVG_W // 2, 36, "Agent Mesh — A2A + LangGraph Architecture", size=18, fill=C_WHITE, weight="bold"))
    parts.append(text(SVG_W // 2, 56, "How research sources become up-to-date Beliefs", size=12, fill=C_SUBTEXT))

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 1: SOURCES (far left column)
    # ═══════════════════════════════════════════════════════════════════════════
    src_x = 30
    src_y = 80
    src_w = 120
    src_h = 36

    parts.append(section_label(src_x, src_y + 14, "SOURCES", C_SUBTEXT))
    parts.append(rect(src_x, src_y + 20, src_w + 10, 320, C_PANEL, rx=8, stroke=C_BORDER))

    sources = [
        ("arXiv", C_BLUE),
        ("Hacker News", C_ORANGE),
        ("GitHub", C_GREY),
        ("Bluesky", C_TEAL),
        ("Reddit", C_RED),
        ("Blogs", C_GREEN),
        ("Leaderboards", C_YELLOW),
    ]
    source_cy = []
    for i, (name, col) in enumerate(sources):
        sy = src_y + 36 + i * 42
        parts.append(rect(src_x + 8, sy, src_w, src_h, C_BG, rx=5, stroke=col))
        parts.append(text(src_x + 8 + src_w // 2, sy + src_h // 2 + 4, name, size=10, fill=col, weight="bold"))
        source_cy.append(sy + src_h // 2)

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 2: SCOUT AGENTS
    # ═══════════════════════════════════════════════════════════════════════════
    sa_x = 200
    sa_y = src_y + 20
    sa_w = 130
    sa_h = 36

    parts.append(section_label(sa_x, src_y + 14, "SCOUT AGENTS  (A2A servers, :8001–:8012)", C_SUBTEXT))
    parts.append(rect(sa_x, sa_y, sa_w + 10, 320, C_PANEL, rx=8, stroke=C_BORDER))

    scouts = [
        ("arxiv-scout", ":8001", C_BLUE),
        ("hn-scout", ":8005", C_ORANGE),
        ("github-scout", ":8008", C_GREY),
        ("bluesky-scout", ":8009", C_TEAL),
        ("reddit-scout", ":8010", C_RED),
        ("blog-scout", ":8011", C_GREEN),
        ("leaderboard-scout", ":8012", C_YELLOW),
    ]
    scout_cy = []
    for i, (name, port, col) in enumerate(scouts):
        sy = sa_y + 16 + i * 42
        parts.append(rect(sa_x + 8, sy, sa_w, sa_h, "#12151f", rx=5, stroke=col))
        parts.append(text(sa_x + 8 + sa_w // 2, sy + 14, name, size=10, fill=col, weight="bold"))
        parts.append(text(sa_x + 8 + sa_w // 2, sy + 27, port, size=8, fill=C_SUBTEXT))
        scout_cy.append(sy + sa_h // 2)

    # arrows: source → scout
    for i, (scy, agy) in enumerate(zip(source_cy, scout_cy)):
        col = sources[i][1]
        parts.append(f'<line x1="{src_x + src_w + 8}" y1="{scy}" x2="{sa_x + 8}" y2="{agy}" stroke="{col}" stroke-width="1" opacity="0.5" marker-end="url(#arr)"/>\n')

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 3: A2A WIRE PROTOCOL
    # ═══════════════════════════════════════════════════════════════════════════
    wp_x = 370
    wp_y = 160
    wp_w = 140
    wp_h = 130

    parts.append(rect(wp_x, wp_y, wp_w, wp_h, "#111827", rx=8, stroke=C_PURPLE))
    parts.append(text(wp_x + wp_w // 2, wp_y + 18, "A2A Wire Protocol", size=10, fill=C_PURPLE, weight="bold"))

    protocol_steps = [
        ("1. GET /.well-known/", "agent-card.json"),
        ("2. POST /mesh/tasks/", "submit → 202 task_id"),
        ("3. GET /mesh/tasks/", "{task_id} → poll"),
        ("4. status=completed", "→ return result"),
    ]
    for i, (l1, l2) in enumerate(protocol_steps):
        py = wp_y + 36 + i * 24
        parts.append(text(wp_x + 8, py, l1, size=8, fill=C_SUBTEXT, anchor="start"))
        parts.append(text(wp_x + 8, py + 11, l2, size=8, fill=C_TEXT, anchor="start"))

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 4: LANGGRAPH COORDINATOR (central)
    # ═══════════════════════════════════════════════════════════════════════════
    lg_x = 540
    lg_y = 75
    lg_w = 380
    lg_h = 680

    parts.append(rect(lg_x, lg_y, lg_w, lg_h, "#0d1220", rx=10, stroke=C_BLUE, stroke_w=2))
    parts.append(text(lg_x + lg_w // 2, lg_y + 20, "LangGraph Coordinator Graph", size=13, fill=C_BLUE, weight="bold"))
    parts.append(text(lg_x + lg_w // 2, lg_y + 35, "coordinator.py  •  thread_id == run_id", size=9, fill=C_SUBTEXT))

    # Graph nodes
    gn_x = lg_x + 30
    gn_w = lg_w - 60
    gn_h = 34

    graph_nodes = [
        ("START", C_GREEN, ""),
        ("scout", C_BLUE, "discover A2A agents, build skill registry"),
        ("fan_scouts →  Send", C_PURPLE, "one Send per scout skill_id (fan-out)"),
        ("scout_one  ×N", C_TEAL, "call scout_arxiv / scout_hn / … via A2A"),
        ("ingest", C_BLUE, "dedup + insert sources into DuckDB"),
        ("route_after_ingest →  Send", C_PURPLE, "one Send per new paper (fan-out)"),
        ("extract_one  ×N", C_TEAL, "LLM claim extraction via claim-extractor"),
        ("track_entities", C_BLUE, "entity resolution + claim insert"),
        ("track_sota", C_ORANGE, "update SOTA beliefs via sota-tracker"),
        ("curate", C_BLUE, "check for open investigations"),
        ("dispatch_investigations", C_ORANGE, "targeted re-scouting for open investigations"),
        ("finalize", C_GREEN, "write PipelineRun, emit metrics"),
        ("END", C_GREEN, ""),
    ]

    node_tops = {}
    node_bots = {}
    gn_y_start = lg_y + 50
    gn_spacing = 46

    for i, (label, col, sub) in enumerate(graph_nodes):
        ny = gn_y_start + i * gn_spacing
        is_edge = label in ("START", "END")
        is_send = "Send" in label
        fill = C_BG if is_edge else ("#1a1d27" if not is_send else "#16102a")
        parts.append(rect(gn_x, ny, gn_w, gn_h, fill, rx=5, stroke=col, stroke_w=2 if is_edge else 1.5))
        lx = gn_x + gn_w // 2
        if sub:
            parts.append(text(lx, ny + 13, label, size=10, fill=col, weight="bold"))
            parts.append(text(lx, ny + 26, sub, size=8, fill=C_SUBTEXT))
        else:
            parts.append(text(lx, ny + gn_h // 2 + 4, label, size=11, fill=col, weight="bold"))
        node_tops[label] = ny
        node_bots[label] = ny + gn_h

    # draw vertical flow arrows between nodes
    flow = [
        ("START", "scout"),
        ("scout", "fan_scouts →  Send"),
        ("fan_scouts →  Send", "scout_one  ×N"),
        ("scout_one  ×N", "ingest"),
        ("ingest", "route_after_ingest →  Send"),
        ("route_after_ingest →  Send", "extract_one  ×N"),
        ("extract_one  ×N", "track_entities"),
        ("track_entities", "track_sota"),
        ("track_sota", "curate"),
        ("curate", "dispatch_investigations"),
        ("dispatch_investigations", "finalize"),
        ("finalize", "END"),
    ]
    cx = gn_x + gn_w // 2
    for a, b in flow:
        y1 = node_bots[a]
        y2 = node_tops[b]
        mid = (y1 + y2) // 2
        parts.append(f'<line x1="{cx}" y1="{y1}" x2="{cx}" y2="{y2}" stroke="{C_BORDER}" stroke-width="1.5" marker-end="url(#arr)"/>\n')

    # conditional skip edge: ingest → finalize (no new papers)
    skip_x = gn_x + gn_w + 12
    parts.append(f'<path d="M {gn_x + gn_w} {node_tops["ingest"] + 17} L {skip_x + 30} {node_tops["ingest"] + 17} L {skip_x + 30} {node_tops["finalize"] + 17} L {gn_x + gn_w} {node_tops["finalize"] + 17}" fill="none" stroke="{C_GREY}" stroke-width="1" stroke-dasharray="4,3" marker-end="url(#arr_grey)"/>\n')
    parts.append(text(skip_x + 32, node_tops["ingest"] + 30, "no new papers", size=7, fill=C_GREY, anchor="start"))

    # conditional skip: extract → finalize (no claims)
    skip2_x = gn_x + gn_w + 50
    parts.append(f'<path d="M {gn_x + gn_w} {node_tops["extract_one  ×N"] + 17} L {skip2_x + 10} {node_tops["extract_one  ×N"] + 17} L {skip2_x + 10} {node_tops["finalize"] + 17} L {gn_x + gn_w} {node_tops["finalize"] + 17}" fill="none" stroke="{C_GREY}" stroke-width="1" stroke-dasharray="4,3" marker-end="url(#arr_grey)"/>\n')
    parts.append(text(skip2_x + 12, node_tops["extract_one  ×N"] + 30, "no claims", size=7, fill=C_GREY, anchor="start"))

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 5: SKEPTIC SWEEP (below coordinator, same column)
    # ═══════════════════════════════════════════════════════════════════════════
    ss_x = lg_x
    ss_y = lg_y + lg_h + 20
    ss_w = lg_w
    ss_h = 140

    # (moved lower — fits under coordinator)
    # We'll describe it in the legend instead and add a compact version

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 6: SPECIALIST AGENTS (right side)
    # ═══════════════════════════════════════════════════════════════════════════
    sp_x = 960
    sp_y = 75
    sp_w = 160
    sp_h = 38

    parts.append(section_label(sp_x, sp_y - 6, "SPECIALIST AGENTS  (A2A servers)", C_SUBTEXT))
    parts.append(rect(sp_x, sp_y, sp_w + 10, 440, C_PANEL, rx=8, stroke=C_BORDER))

    specialists = [
        ("claim-extractor", ":8002", C_TEAL,   "LLM: extract claims"),
        ("entity-tracker",  ":8003", C_ORANGE,  "resolve + find/create entities"),
        ("sota-tracker",    ":8004", C_ORANGE,  "update SOTA beliefs"),
        ("skeptic",         ":8006", C_RED,     "LLM: challenge beliefs"),
        ("curator",         ":8007", C_PURPLE,  "LLM: pick beliefs to challenge"),
        ("investigation-*", ":850x", C_YELLOW,  "targeted re-scouting"),
    ]
    spec_cy = {}
    for i, (name, port, col, desc) in enumerate(specialists):
        sy = sp_y + 20 + i * 64
        parts.append(rect(sp_x + 8, sy, sp_w, sp_h + 10, "#12151f", rx=5, stroke=col))
        parts.append(text(sp_x + 8 + sp_w // 2, sy + 14, name, size=10, fill=col, weight="bold"))
        parts.append(text(sp_x + 8 + sp_w // 2, sy + 27, port, size=8, fill=C_SUBTEXT))
        parts.append(text(sp_x + 8 + sp_w // 2, sy + 40, desc, size=8, fill=C_SUBTEXT))
        spec_cy[name] = sy + (sp_h + 10) // 2

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 7: DATA STORES
    # ═══════════════════════════════════════════════════════════════════════════
    ds_x = 960
    ds_y = sp_y + 450
    ds_w = 160

    parts.append(section_label(ds_x, ds_y - 8, "DATA STORES", C_SUBTEXT))

    # DuckDB
    parts.append(rect(ds_x, ds_y, ds_w, 60, C_PANEL, rx=6, stroke=C_DUCKDB))
    parts.append(text(ds_x + ds_w // 2, ds_y + 20, "DuckDB  (mesh.db)", size=10, fill=C_DUCKDB, weight="bold"))
    parts.append(text(ds_x + ds_w // 2, ds_y + 33, "Sources · Claims · Entities", size=8, fill=C_SUBTEXT))
    parts.append(text(ds_x + ds_w // 2, ds_y + 46, "Beliefs · Revisions · Runs", size=8, fill=C_SUBTEXT))

    # Postgres checkpoint
    parts.append(rect(ds_x, ds_y + 72, ds_w, 50, C_PANEL, rx=6, stroke=C_POSTGRES))
    parts.append(text(ds_x + ds_w // 2, ds_y + 72 + 18, "Postgres  (langgraph-db)", size=9, fill=C_POSTGRES, weight="bold"))
    parts.append(text(ds_x + ds_w // 2, ds_y + 72 + 32, "LangGraph checkpoints", size=8, fill=C_SUBTEXT))
    parts.append(text(ds_x + ds_w // 2, ds_y + 72 + 44, "(thread_id = run_id)", size=8, fill=C_SUBTEXT))

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 8: SKEPTIC SWEEP (separate graph, right side lower)
    # ═══════════════════════════════════════════════════════════════════════════
    sw_x = 960
    sw_y = ds_y + 72 + 62
    sw_w = 390
    sw_h = 180

    parts.append(rect(sw_x, sw_y, sw_w, sw_h, "#0d1220", rx=8, stroke=C_RED, stroke_w=2))
    parts.append(text(sw_x + sw_w // 2, sw_y + 18, "LangGraph Skeptic Sweep Graph", size=12, fill=C_RED, weight="bold"))
    parts.append(text(sw_x + sw_w // 2, sw_y + 32, "skeptic_sweep.py  •  out-of-band falsification", size=9, fill=C_SUBTEXT))

    sw_nodes = [
        ("START", C_GREEN),
        ("load_beliefs  →  Curator A2A call", C_PURPLE),
        ("Send fan-out: evaluate_one ×N", C_RED),
        ("  Skeptic A2A call per belief  ", C_RED),
        ("trigger_curator  (if contradiction)", C_PURPLE),
        ("finalize", C_GREEN),
        ("END", C_GREEN),
    ]
    sn_x = sw_x + 20
    sn_w = sw_w - 40
    sn_h = 18
    sn_spacing = 21
    sn_y0 = sw_y + 45
    for i, (lbl, col) in enumerate(sw_nodes):
        ny = sn_y0 + i * sn_spacing
        parts.append(rect(sn_x, ny, sn_w, sn_h, C_BG, rx=3, stroke=col, stroke_w=1))
        parts.append(text(sn_x + sn_w // 2, ny + 13, lbl, size=9, fill=col))
        if i < len(sw_nodes) - 1:
            parts.append(f'<line x1="{sn_x + sn_w//2}" y1="{ny+sn_h}" x2="{sn_x + sn_w//2}" y2="{ny+sn_spacing}" stroke="{C_BORDER}" stroke-width="1" marker-end="url(#arr)"/>\n')

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 9: API / WIKI / SCHEDULER
    # ═══════════════════════════════════════════════════════════════════════════
    cons_x = 30
    cons_y = 470
    cons_w = 480
    cons_h = 120

    parts.append(rect(cons_x, cons_y, cons_w, cons_h, C_PANEL, rx=8, stroke=C_BORDER))
    parts.append(section_label(cons_x + 10, cons_y + 16, "CONSUMERS / TRIGGERS", C_SUBTEXT))

    consumers = [
        (cons_x + 20,  cons_y + 30, 120, 40, "APScheduler", "mesh-scheduler\ncron triggers", C_ORANGE),
        (cons_x + 160, cons_y + 30, 120, 40, "FastAPI :8000", "read-only REST API\n/api/v1/*", C_TEAL),
        (cons_x + 300, cons_y + 30, 120, 40, "Next.js :3000", "wiki (server\ncomponents)", C_GREEN),
        (cons_x + 20,  cons_y + 78, 120, 32, "CLI  mesh.cli", "manual triggers", C_BLUE),
        (cons_x + 160, cons_y + 78, 260, 32, "/status  →  reads LangGraph checkpoints (Postgres)", "", C_PURPLE),
    ]
    for cx2, cy2, cw, ch, lbl, sub, col in consumers:
        parts.append(rect(cx2, cy2, cw, ch, C_BG, rx=4, stroke=col))
        parts.append(text(cx2 + cw // 2, cy2 + 13, lbl, size=9, fill=col, weight="bold"))
        if sub:
            for j, line2 in enumerate(sub.split("\n")):
                parts.append(text(cx2 + cw // 2, cy2 + 24 + j * 10, line2, size=7, fill=C_SUBTEXT))

    # ═══════════════════════════════════════════════════════════════════════════
    # SECTION 10: COORDINATOR → A2A arrows
    # ═══════════════════════════════════════════════════════════════════════════

    coord_right_x = lg_x + lg_w

    # coordinator → claim-extractor  (extract_one)
    ey = node_tops["extract_one  ×N"] + 17
    parts.append(arrow(coord_right_x, ey, sp_x + 8, spec_cy["claim-extractor"], C_TEAL, w=1.5, marker="url(#arr_teal)"))
    parts.append(text((coord_right_x + sp_x + 8) // 2, ey - 6, "extract_claims", size=8, fill=C_TEAL))

    # coordinator → entity-tracker  (track_entities)
    ey2 = node_tops["track_entities"] + 17
    parts.append(arrow(coord_right_x, ey2, sp_x + 8, spec_cy["entity-tracker"], C_ORANGE, w=1.5, marker="url(#arr_orange)"))
    parts.append(text((coord_right_x + sp_x + 8) // 2, ey2 - 6, "resolve_entities", size=8, fill=C_ORANGE))

    # coordinator → sota-tracker
    ey3 = node_tops["track_sota"] + 17
    parts.append(arrow(coord_right_x, ey3, sp_x + 8, spec_cy["sota-tracker"], C_ORANGE, w=1.5, marker="url(#arr_orange)"))
    parts.append(text((coord_right_x + sp_x + 8) // 2, ey3 - 6, "update_sota", size=8, fill=C_ORANGE))

    # coordinator → investigation scouts (dispatch_investigations)
    ey4 = node_tops["dispatch_investigations"] + 17
    parts.append(arrow(coord_right_x, ey4, sp_x + 8, spec_cy["investigation-*"], C_YELLOW, w=1.5, marker="url(#arr)"))
    parts.append(text((coord_right_x + sp_x + 8) // 2, ey4 - 6, "investigate_*", size=8, fill=C_YELLOW))

    # skeptic sweep → skeptic + curator
    sw_mid_x = sw_x + sw_w // 2
    parts.append(arrow(sw_mid_x, sw_y, sp_x + 8 + sp_w // 2, spec_cy["curator"] + 20, C_PURPLE, w=1.5, dash="4,3", marker="url(#arr_purple)"))
    parts.append(arrow(sw_mid_x, sw_y + 30, sp_x + 8 + sp_w // 2, spec_cy["skeptic"] + 20, C_RED, w=1.5, dash="4,3", marker="url(#arr)"))

    # coordinator → DuckDB
    coord_bottom = lg_y + lg_h
    coord_mid_x = lg_x + lg_w // 2
    parts.append(arrow(coord_mid_x, coord_bottom, ds_x + ds_w // 2, ds_y, C_DUCKDB, w=1.5, dash="5,3", marker="url(#arr_orange)"))
    parts.append(text(coord_mid_x + 30, coord_bottom + 30, "reads + writes", size=8, fill=C_DUCKDB))

    # coordinator → Postgres checkpoint
    parts.append(arrow(lg_x + lg_w - 30, lg_y + lg_h // 2, ds_x, ds_y + 72 + 25, C_POSTGRES, w=1.5, dash="3,3", marker="url(#arr_grey)"))
    parts.append(text(ds_x - 60, ds_y + 72 + 10, "checkpoint", size=8, fill=C_POSTGRES))
    parts.append(text(ds_x - 60, ds_y + 72 + 21, "each node", size=8, fill=C_POSTGRES))

    # scouts → coordinator (via fan-out arrows)
    sa_right_x = sa_x + sa_w + 8
    coord_left_x = lg_x
    scout_mid_y = sa_y + 160
    parts.append(f'<path d="M {sa_right_x} {scout_mid_y} L {wp_x} {scout_mid_y}" fill="none" stroke="{C_BLUE}" stroke-width="1.5" stroke-dasharray="5,3" marker-end="url(#arr)"/>\n')
    parts.append(f'<path d="M {wp_x + wp_w} {scout_mid_y} L {coord_left_x} {node_tops["scout_one  ×N"] + 17}" fill="none" stroke="{C_BLUE}" stroke-width="1.5" marker-end="url(#arr)"/>\n')

    # scheduler → coordinator
    sched_cx = cons_x + 80
    sched_bot = cons_y + 78
    parts.append(arrow(sched_cx, sched_bot, lg_x + 40, lg_y + lg_h, C_ORANGE, w=1.5, dash="4,3", marker="url(#arr_orange)"))
    parts.append(text(sched_cx - 30, sched_bot + 30, "trigger run", size=8, fill=C_ORANGE))

    # API → DuckDB
    api_cx = cons_x + 220
    parts.append(arrow(api_cx, cons_y + cons_h, ds_x + 60, ds_y + 60, C_TEAL, w=1, dash="3,3", marker="url(#arr_teal)"))

    # ═══════════════════════════════════════════════════════════════════════════
    # LEGEND
    # ═══════════════════════════════════════════════════════════════════════════
    leg_x = 30
    leg_y = SVG_H - 130
    parts.append(rect(leg_x, leg_y, 880, 100, C_PANEL, rx=6, stroke=C_BORDER))
    parts.append(text(leg_x + 10, leg_y + 16, "Legend", size=10, fill=C_SUBTEXT, anchor="start", weight="bold"))

    legend_items = [
        (C_BLUE,     "LangGraph graph node"),
        (C_PURPLE,   "Send fan-out (parallel dispatch)"),
        (C_TEAL,     "LLM-backed specialist agent"),
        (C_ORANGE,   "Rule-based specialist / DB write"),
        (C_RED,      "Skeptic sweep path"),
        (C_YELLOW,   "Investigation dispatch"),
        (C_GREY,     "Conditional skip edge"),
        (C_POSTGRES, "LangGraph checkpoint store"),
        (C_DUCKDB,   "Primary knowledge store"),
    ]
    cols = 3
    items_per_col = 3
    for i, (col, lbl) in enumerate(legend_items):
        ci = i % cols
        ri = i // cols
        lx2 = leg_x + 14 + ci * 295
        ly2 = leg_y + 32 + ri * 22
        parts.append(rect(lx2, ly2 - 10, 12, 12, col, rx=2))
        parts.append(text(lx2 + 18, ly2, lbl, size=9, fill=C_TEXT, anchor="start"))

    parts.append("</svg>")
    return "".join(parts)


if __name__ == "__main__":
    svg = build_svg()
    out_path = "/Users/benklassen/agent_mesh/architecture.svg"
    with open(out_path, "w") as f:
        f.write(svg)
    print(f"Written: {out_path}")

'use client';

/**
 * MeshPulse — a live, animated view of the controller actually working.
 *
 * The controller writes one `agent_invocations` row per skill dispatch. Each row
 * carries the skill that fired, the tension `kind` that triggered it, the
 * `outcome` (effects / no_effects / error), an effect count, and cost. We poll
 * the `/api/v1/agents/activity` firehose (incrementally, via a `since` cursor)
 * and turn every new invocation into a glowing packet that travels the real
 * substrate loop:
 *
 *     feeds → [scout] → sources → [extract] → claims → [synthesize] → KNOWLEDGE
 *     BASE, with the skeptic / maintenance family orbiting the core, and a
 *     gap → investigation → back-to-sources feedback arc closing the loop.
 *
 * Packet colour encodes the outcome (did the activation change anything?); the
 * channel hue encodes the skill's function. Nodes glow with recent activity.
 * No websockets exist server-side — this is polling + requestAnimationFrame.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { api, type AgentInvocation, type Stats } from '@/lib/api';

// ---------------------------------------------------------------------------
// Geometry — the substrate nodes laid out around the central Knowledge Base.
// viewBox is 1000×640; every position is in that space.
// ---------------------------------------------------------------------------

type Pt = { x: number; y: number };

const KB = { x: 500, y: 322, r: 76 };

const NODES: Record<string, { x: number; y: number; r: number; label: string; sub: string }> = {
  world: { x: 132, y: 150, r: 34, label: 'Feeds', sub: 'connectors' },
  sources: { x: 108, y: 372, r: 34, label: 'Sources', sub: 'raw' },
  claims: { x: 248, y: 556, r: 34, label: 'Claims', sub: 'extracted' },
  entities: { x: 512, y: 588, r: 30, label: 'Entities', sub: 'resolved' },
  investigations: { x: 872, y: 470, r: 32, label: 'Investigations', sub: 'open' },
  memory: { x: 878, y: 168, r: 30, label: 'Memory', sub: 'learned' },
  brief: { x: 512, y: 78, r: 26, label: 'Brief', sub: 'digest' },
  beliefs: { x: KB.x, y: KB.y, r: KB.r, label: 'Knowledge Base', sub: 'beliefs' },
};

// Function groups → base hue for a channel's line + label.
const GROUP_HUE: Record<string, string> = {
  gather: '#38bdf8', // sky — pull raw material in
  extract: '#a78bfa', // violet — turn sources into claims
  resolve: '#f472b6', // pink — entity resolution
  build: '#34d399', // emerald — synthesise beliefs
  inquire: '#fbbf24', // amber — open investigations from gaps
  critique: '#fb923c', // orange — skeptic / adjudication
  maintain: '#94a3b8', // slate — decay, consolidation, memory
  publish: '#2dd4bf', // teal — field brief
};

type Channel = {
  label: string;
  group: keyof typeof GROUP_HUE;
  kind: 'edge' | 'orbit';
  from?: string;
  to?: string;
  bow?: number; // perpendicular bow of the edge, in px (signed)
  orbitR?: number; // orbit radius for kind === 'orbit'
};

// skill_id (== agent_invocations.skill) → how it moves on the board.
const CHANNELS: Record<string, Channel> = {
  'scout-source': { label: 'scout', group: 'gather', kind: 'edge', from: 'world', to: 'sources', bow: -34 },
  'extract-source': { label: 'extract', group: 'extract', kind: 'edge', from: 'sources', to: 'claims', bow: -46 },
  'synthesize-belief': { label: 'synthesize', group: 'build', kind: 'edge', from: 'claims', to: 'beliefs', bow: -30 },
  'merge-candidate': { label: 'resolve', group: 'resolve', kind: 'edge', from: 'claims', to: 'entities', bow: 26 },
  'investigate-gap': { label: 'investigate', group: 'inquire', kind: 'edge', from: 'beliefs', to: 'investigations', bow: -24 },
  'dispatch-investigation': { label: 'dispatch', group: 'gather', kind: 'edge', from: 'investigations', to: 'sources', bow: 150 },
  'consolidate-memory': { label: 'remember', group: 'maintain', kind: 'edge', from: 'beliefs', to: 'memory', bow: -30 },
  'write-field-brief': { label: 'brief', group: 'publish', kind: 'edge', from: 'beliefs', to: 'brief', bow: 18 },
  'challenge-belief': { label: 'challenge', group: 'critique', kind: 'orbit', orbitR: 104 },
  'adjudicate-contradiction': { label: 'adjudicate', group: 'critique', kind: 'orbit', orbitR: 120 },
  'consolidate-beliefs': { label: 'consolidate', group: 'maintain', kind: 'orbit', orbitR: 136 },
  'maintain-belief': { label: 'maintain', group: 'maintain', kind: 'orbit', orbitR: 152 },
};

// A skill we don't have a channel for still shows up — as an orbit pulse on the core.
function channelFor(skill: string): Channel {
  return CHANNELS[skill] ?? { label: skill, group: 'maintain', kind: 'orbit', orbitR: 88 };
}

const OUTCOME_COLOR: Record<string, string> = {
  effects: '#34d399', // green — it changed the world
  no_effects: '#fbbf24', // amber — ran, produced nothing (a stall signal)
  error: '#f87171', // red — raised
};
function outcomeColor(o: string): string {
  return OUTCOME_COLOR[o] ?? '#7dd3fc';
}

// A static 2nd support edge (entities feed the KB) — drawn, never a channel.
const SUPPORT_EDGES: Array<{ from: string; to: string; bow: number }> = [
  { from: 'entities', to: 'beliefs', bow: 30 },
];

// ---------------------------------------------------------------------------
// Bezier helpers — an edge is a quadratic bezier A → control → B.
// ---------------------------------------------------------------------------

function controlPoint(a: Pt, b: Pt, bow: number): Pt {
  const mx = (a.x + b.x) / 2;
  const my = (a.y + b.y) / 2;
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len = Math.hypot(dx, dy) || 1;
  // unit normal
  const nx = -dy / len;
  const ny = dx / len;
  return { x: mx + nx * bow, y: my + ny * bow };
}

function quad(a: Pt, c: Pt, b: Pt, t: number): Pt {
  const mt = 1 - t;
  return {
    x: mt * mt * a.x + 2 * mt * t * c.x + t * t * b.x,
    y: mt * mt * a.y + 2 * mt * t * c.y + t * t * b.y,
  };
}

function edgeEndpoints(ch: Channel): { a: Pt; b: Pt; c: Pt } {
  const from = NODES[ch.from ?? 'beliefs'];
  const to = NODES[ch.to ?? 'beliefs'];
  const a = { x: from.x, y: from.y };
  const b = { x: to.x, y: to.y };
  const c = controlPoint(a, b, ch.bow ?? 0);
  // Trim the path to the node rims so packets start/end at the edges, not centres.
  const start = trimToRim(a, c, from.r + 2);
  const end = trimToRim(b, c, to.r + 2);
  return { a: start, b: end, c };
}

function trimToRim(center: Pt, toward: Pt, r: number): Pt {
  const dx = toward.x - center.x;
  const dy = toward.y - center.y;
  const len = Math.hypot(dx, dy) || 1;
  return { x: center.x + (dx / len) * r, y: center.y + (dy / len) * r };
}

function pathD(ch: Channel): string {
  const { a, b, c } = edgeEndpoints(ch);
  return `M ${a.x} ${a.y} Q ${c.x} ${c.y} ${b.x} ${b.y}`;
}

// Where a packet sits at progress t (0→1) along its channel.
function packetPos(ch: Channel, t: number, baseAngle: number): Pt {
  if (ch.kind === 'orbit') {
    const ang = baseAngle + t * Math.PI * 2;
    const r = ch.orbitR ?? 100;
    return { x: KB.x + Math.cos(ang) * r, y: KB.y + Math.sin(ang) * r };
  }
  const { a, b, c } = edgeEndpoints(ch);
  return quad(a, c, b, t);
}

// ---------------------------------------------------------------------------
// Live data types
// ---------------------------------------------------------------------------

type Summary = { outcome: string; effects: number; kind: string; tier: string };
function summaryOf(inv: AgentInvocation): Summary {
  const s = (inv.output_summary ?? {}) as Record<string, unknown>;
  return {
    outcome: String(s.outcome ?? 'effects'),
    effects: Number(s.effects ?? 0),
    kind: String(s.kind ?? ''),
    tier: String(s.tier ?? ''),
  };
}

type Packet = {
  key: string;
  skill: string;
  color: string;
  born: number; // performance.now() when it starts moving
  dur: number;
  baseAngle: number;
  historical: boolean;
};

type TickItem = {
  id: string;
  skill: string;
  label: string;
  kind: string;
  outcome: string;
  effects: number;
  cost: number;
  at: number; // ms epoch of created_at
};

const PACKET_DUR = 1700;
const POLL_MS = 2500;
const STATS_MS = 11000;

// ---------------------------------------------------------------------------

export function MeshPulse({ field, initial }: { field: string; initial: AgentInvocation[] }) {
  const [stats, setStats] = useState<Stats | null>(null);
  const [ticks, setTicks] = useState<TickItem[]>([]);
  const [sessionCount, setSessionCount] = useState(0);
  const [sessionCost, setSessionCost] = useState(0);
  const [connected, setConnected] = useState(true);
  const [, forceFrame] = useState(0);

  const packetsRef = useRef<Packet[]>([]);
  const heatRef = useRef<Record<string, number>>({});
  const coreRef = useRef(0); // extra pulse of the KB core
  const cursorRef = useRef<string | null>(null);
  const seenRef = useRef<Set<string>>(new Set());
  const spawnCounter = useRef(0);

  const bumpHeat = useCallback((id: string, amount: number) => {
    heatRef.current[id] = Math.min(1.6, (heatRef.current[id] ?? 0) + amount);
  }, []);

  // Turn one invocation into a moving packet + ticker row + counters.
  const enqueue = useCallback(
    (inv: AgentInvocation, opts: { historical?: boolean; delay?: number } = {}) => {
      const id = inv.id ?? '';
      const created = inv.created_at ?? '';
      if (!id || seenRef.current.has(id)) return;
      seenRef.current.add(id);
      if (seenRef.current.size > 4000) {
        // keep the set bounded
        seenRef.current = new Set(Array.from(seenRef.current).slice(-2000));
      }
      const s = summaryOf(inv);
      const ch = channelFor(inv.skill);
      const now = performance.now();
      const jitter = (spawnCounter.current++ % 7) * (Math.PI / 7);
      packetsRef.current.push({
        key: `${id}:${spawnCounter.current}`,
        skill: inv.skill,
        color: outcomeColor(s.outcome),
        born: now + (opts.delay ?? 0),
        dur: PACKET_DUR,
        baseAngle: jitter,
        historical: !!opts.historical,
      });

      const item: TickItem = {
        id,
        skill: inv.skill,
        label: ch.label,
        kind: s.kind,
        outcome: s.outcome,
        effects: s.effects,
        cost: inv.cost_usd ?? 0,
        at: created ? Date.parse(created) : Date.now(),
      };
      setTicks((prev) => [item, ...prev].slice(0, 16));
      if (!opts.historical) {
        setSessionCount((c) => c + 1);
        setSessionCost((c) => c + (inv.cost_usd ?? 0));
      }
    },
    [],
  );

  // Advance the cursor to the newest row we've seen (ISO strings sort correctly).
  const advanceCursor = useCallback((rows: AgentInvocation[]) => {
    for (const r of rows) {
      const ts = r.created_at;
      if (!ts) continue;
      if (cursorRef.current === null || ts > cursorRef.current) {
        cursorRef.current = ts;
      }
    }
  }, []);

  // Seed: replay the most-recent invocations as a staggered burst so the board
  // comes alive immediately with REAL data, then hand off to live polling.
  useEffect(() => {
    advanceCursor(initial);
    const recent = [...initial].reverse().slice(-26); // oldest → newest
    recent.forEach((inv, i) => enqueue(inv, { historical: true, delay: i * 260 }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Live poll of the activity firehose (incremental via the `since` cursor).
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const rows = await api.agentActivity(field, cursorRef.current ?? undefined, 80);
        if (!alive) return;
        setConnected(true);
        if (rows.length) {
          advanceCursor(rows);
          // API returns newest-first; play oldest→newest, lightly staggered.
          [...rows].reverse().forEach((inv, i) => enqueue(inv, { delay: i * 200 }));
        }
      } catch {
        if (alive) setConnected(false);
      }
    };
    const h = setInterval(tick, POLL_MS);
    return () => {
      alive = false;
      clearInterval(h);
    };
  }, [field, enqueue, advanceCursor]);

  // Live poll of the substrate counts.
  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const s = await api.stats();
        if (alive) setStats(s);
      } catch {
        /* keep last */
      }
    };
    load();
    const h = setInterval(load, STATS_MS);
    return () => {
      alive = false;
      clearInterval(h);
    };
  }, [field]);

  // Animation heartbeat: prune finished packets, bump/decay node heat, re-render.
  useEffect(() => {
    let raf = 0;
    let lastPaint = 0;
    const arrivalDone = new Set<string>();
    const frame = (now: number) => {
      const live: Packet[] = [];
      for (const p of packetsRef.current) {
        const t = (now - p.born) / p.dur;
        if (t < 0) {
          live.push(p);
          continue;
        }
        if (t >= 1) {
          // arrival: light up the destination
          const ch = channelFor(p.skill);
          if (ch.kind === 'edge') bumpHeat(ch.to ?? 'beliefs', 0.9);
          else coreRef.current = Math.min(1.8, coreRef.current + 0.5);
          if (p.skill === 'synthesize-belief') coreRef.current = Math.min(2.2, coreRef.current + 0.7);
          continue;
        }
        // departure spark, once
        if (t > 0.04 && !arrivalDone.has(p.key)) {
          arrivalDone.add(p.key);
          const ch = channelFor(p.skill);
          if (ch.kind === 'edge') bumpHeat(ch.from ?? 'beliefs', 0.55);
        }
        live.push(p);
      }
      packetsRef.current = live;
      // decay heat
      for (const k of Object.keys(heatRef.current)) {
        heatRef.current[k] *= 0.945;
        if (heatRef.current[k] < 0.02) delete heatRef.current[k];
      }
      coreRef.current *= 0.95;
      if (arrivalDone.size > 500) arrivalDone.clear();
      if (now - lastPaint > 33) {
        lastPaint = now;
        forceFrame((f) => (f + 1) % 1_000_000);
      }
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [bumpHeat]);

  // Rolling activations-per-minute from real created_at timestamps.
  const perMin = useMemo(() => {
    const cutoff = Date.now() - 60_000;
    return ticks.filter((t) => t.at >= cutoff).length;
  }, [ticks]);

  const now = performance.now();
  const activePackets = packetsRef.current
    .map((p) => {
      const t = (now - p.born) / p.dur;
      if (t < 0 || t >= 1) return null;
      const ch = channelFor(p.skill);
      const pos = packetPos(ch, t, p.baseAngle);
      return { p, t, pos };
    })
    .filter((x): x is NonNullable<typeof x> => x !== null);

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_320px]">
      <div className="relative overflow-hidden rounded-xl border border-slate-800 bg-[#0a0f1c]">
        <PulseBoard
          activePackets={activePackets}
          heat={heatRef.current}
          core={coreRef.current}
          stats={stats}
        />
        <div className="pointer-events-none absolute left-3 top-3 flex items-center gap-2 text-[11px] font-medium text-slate-400">
          <span
            className={`inline-block h-2 w-2 rounded-full ${connected ? 'bg-emerald-400' : 'bg-amber-400'}`}
            style={connected ? { boxShadow: '0 0 8px #34d399' } : undefined}
          />
          {connected ? 'live' : 'reconnecting…'}
          <span className="text-slate-600">·</span>
          <span className="text-slate-500">topic {field}</span>
        </div>
        {activePackets.length === 0 && ticks.length === 0 && (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
            <p className="text-sm text-slate-500">Waiting for the controller to act…</p>
          </div>
        )}
      </div>

      <SidePanel
        stats={stats}
        ticks={ticks}
        sessionCount={sessionCount}
        sessionCost={sessionCost}
        perMin={perMin}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// The SVG board
// ---------------------------------------------------------------------------

function PulseBoard({
  activePackets,
  heat,
  core,
  stats,
}: {
  activePackets: Array<{ p: Packet; t: number; pos: Pt }>;
  heat: Record<string, number>;
  core: number;
  stats: Stats | null;
}) {
  const orbitChannels = Object.entries(CHANNELS).filter(([, c]) => c.kind === 'orbit');
  const edgeChannels = Object.entries(CHANNELS).filter(([, c]) => c.kind === 'edge');

  const nodeCount: Record<string, number | undefined> = {
    sources: stats?.sources,
    claims: stats?.claims,
    entities: stats?.entities,
    beliefs: stats?.beliefs,
  };

  return (
    <svg viewBox="0 0 1000 640" className="block w-full" role="img" aria-label="Live agent mesh activity">
      <defs>
        <radialGradient id="kbCore" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#5eead4" stopOpacity="0.95" />
          <stop offset="45%" stopColor="#2dd4bf" stopOpacity="0.35" />
          <stop offset="100%" stopColor="#0f766e" stopOpacity="0.05" />
        </radialGradient>
        <filter id="glow" x="-120%" y="-120%" width="340%" height="340%">
          <feGaussianBlur stdDeviation="4" result="b" />
          <feMerge>
            <feMergeNode in="b" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
        <filter id="soft" x="-80%" y="-80%" width="260%" height="260%">
          <feGaussianBlur stdDeviation="7" />
        </filter>
      </defs>

      {/* faint concentric rings behind the core */}
      {[190, 150, 110].map((r) => (
        <circle key={r} cx={KB.x} cy={KB.y} r={r} fill="none" stroke="#1e293b" strokeWidth={1} />
      ))}

      {/* orbit rings (skeptic / maintenance family) */}
      {orbitChannels.map(([id, c]) => (
        <circle
          key={id}
          cx={KB.x}
          cy={KB.y}
          r={c.orbitR}
          fill="none"
          stroke={GROUP_HUE[c.group]}
          strokeOpacity={0.14}
          strokeWidth={1}
          strokeDasharray="2 6"
        />
      ))}

      {/* support edges (static structure) */}
      {SUPPORT_EDGES.map((e, i) => (
        <path
          key={`sup-${i}`}
          d={pathD({ label: '', group: 'maintain', kind: 'edge', from: e.from, to: e.to, bow: e.bow })}
          fill="none"
          stroke="#1e293b"
          strokeWidth={1.4}
        />
      ))}

      {/* channel edges + labels */}
      {edgeChannels.map(([id, c]) => {
        const active = (heat[c.to ?? ''] ?? 0) + (heat[c.from ?? ''] ?? 0);
        const hue = GROUP_HUE[c.group];
        const mid = packetPos(c, 0.5, 0);
        return (
          <g key={id}>
            <path
              d={pathD(c)}
              fill="none"
              stroke={hue}
              strokeOpacity={0.18 + Math.min(0.5, active * 0.4)}
              strokeWidth={1.6}
            />
            <text
              x={mid.x}
              y={mid.y - 6}
              textAnchor="middle"
              className="select-none"
              fontSize={11}
              fill={hue}
              fillOpacity={0.5 + Math.min(0.5, active)}
              style={{ fontWeight: 500 }}
            >
              {c.label}
            </text>
          </g>
        );
      })}

      {/* nodes */}
      {Object.entries(NODES).map(([id, n]) => {
        if (id === 'beliefs') return null; // KB drawn separately
        const h = heat[id] ?? 0;
        return (
          <g key={id}>
            {h > 0.05 && (
              <circle cx={n.x} cy={n.y} r={n.r + 10 + h * 14} fill="#38bdf8" opacity={h * 0.18} filter="url(#soft)" />
            )}
            <circle
              cx={n.x}
              cy={n.y}
              r={n.r}
              fill="#0f1a2e"
              stroke={h > 0.05 ? '#7dd3fc' : '#334155'}
              strokeWidth={1.5 + h * 2}
            />
            <text x={n.x} y={n.y - 1} textAnchor="middle" fontSize={12} fill="#e2e8f0" style={{ fontWeight: 600 }}>
              {nodeCount[id] !== undefined ? compact(nodeCount[id]!) : n.label}
            </text>
            <text x={n.x} y={n.y + 13} textAnchor="middle" fontSize={9} fill="#64748b">
              {nodeCount[id] !== undefined ? n.label.toLowerCase() : n.sub}
            </text>
          </g>
        );
      })}

      {/* the Knowledge Base core */}
      <g>
        <circle
          cx={KB.x}
          cy={KB.y}
          r={KB.r + 20 + core * 18}
          fill="url(#kbCore)"
          opacity={0.55 + Math.min(0.4, core * 0.3)}
        />
        <circle cx={KB.x} cy={KB.y} r={KB.r} fill="#08201d" stroke="#2dd4bf" strokeWidth={2 + core * 2} />
        <text x={KB.x} y={KB.y - 8} textAnchor="middle" fontSize={26} fill="#5eead4" style={{ fontWeight: 700 }}>
          {stats ? compact(stats.beliefs) : '·'}
        </text>
        <text x={KB.x} y={KB.y + 14} textAnchor="middle" fontSize={11} fill="#99f6e4" style={{ letterSpacing: 0.5 }}>
          KNOWLEDGE BASE
        </text>
        <text x={KB.x} y={KB.y + 30} textAnchor="middle" fontSize={9} fill="#5eead4" fillOpacity={0.7}>
          beliefs
        </text>
      </g>

      {/* moving packets */}
      {activePackets.map(({ p, t, pos }) => {
        const fade = t < 0.12 ? t / 0.12 : t > 0.88 ? (1 - t) / 0.12 : 1;
        const op = (p.historical ? 0.5 : 1) * fade;
        return (
          <g key={p.key} filter="url(#glow)">
            <circle cx={pos.x} cy={pos.y} r={5.5} fill={p.color} opacity={op} />
            <circle cx={pos.x} cy={pos.y} r={2.4} fill="#f8fafc" opacity={op * 0.9} />
          </g>
        );
      })}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Side panel — counters, legend, live ticker
// ---------------------------------------------------------------------------

function SidePanel({
  stats,
  ticks,
  sessionCount,
  sessionCost,
  perMin,
}: {
  stats: Stats | null;
  ticks: TickItem[];
  sessionCount: number;
  sessionCost: number;
  perMin: number;
}) {
  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-2">
        <Metric label="activations / min" value={String(perMin)} />
        <Metric label="this session" value={compact(sessionCount)} />
        <Metric label="session cost" value={`$${sessionCost.toFixed(sessionCost < 1 ? 4 : 2)}`} />
        <Metric
          label="last run"
          value={stats?.last_pipeline_run_at ? timeAgo(Date.parse(stats.last_pipeline_run_at)) : '—'}
        />
      </div>

      <Legend />

      <div className="rounded-lg border border-border bg-card">
        <div className="border-b border-border px-3 py-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Activity feed
        </div>
        <ul className="max-h-[360px] divide-y divide-border overflow-y-auto">
          {ticks.length === 0 && (
            <li className="px-3 py-6 text-center text-xs text-muted-foreground">No activations yet.</li>
          )}
          {ticks.map((t) => (
            <li key={t.id} className="flex items-center gap-2 px-3 py-2 text-xs">
              <span
                className="mt-0.5 inline-block h-2 w-2 shrink-0 rounded-full"
                style={{ background: outcomeColor(t.outcome) }}
                title={t.outcome}
              />
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline justify-between gap-2">
                  <span className="truncate font-medium text-foreground">{t.skill}</span>
                  <span className="shrink-0 tabular-nums text-muted-foreground">{timeAgo(t.at)}</span>
                </div>
                <div className="truncate text-muted-foreground">
                  {t.kind ? kindLabel(t.kind) : t.label}
                  {t.outcome === 'effects' && t.effects > 0 ? ` · +${t.effects}` : ''}
                  {t.outcome === 'no_effects' ? ' · no change' : ''}
                  {t.outcome === 'error' ? ' · error' : ''}
                </div>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border bg-card px-3 py-2">
      <div className="tabular-nums text-lg font-semibold text-foreground">{value}</div>
      <div className="text-[11px] leading-tight text-muted-foreground">{label}</div>
    </div>
  );
}

function Legend() {
  return (
    <div className="rounded-lg border border-border bg-card px-3 py-2.5 text-xs">
      <div className="mb-1.5 font-semibold text-foreground">Packet = one activation</div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-muted-foreground">
        <LegendDot color={OUTCOME_COLOR.effects} label="changed the KB" />
        <LegendDot color={OUTCOME_COLOR.no_effects} label="no change" />
        <LegendDot color={OUTCOME_COLOR.error} label="error" />
      </div>
      <p className="mt-2 leading-snug text-muted-foreground">
        Each skill fires in response to a tension. Packets flow feeds → sources →
        claims → the knowledge base; the skeptic and maintenance skills orbit the
        core.
      </p>
    </div>
  );
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="inline-block h-2 w-2 rounded-full" style={{ background: color }} />
      {label}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

function compact(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

function timeAgo(ms: number): string {
  if (!Number.isFinite(ms)) return '—';
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 5) return 'now';
  if (s < 60) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.round(h / 24)}d`;
}

function kindLabel(kind: string): string {
  return kind.replace(/_/g, ' ');
}

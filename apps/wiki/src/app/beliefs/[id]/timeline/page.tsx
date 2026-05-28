import Link from 'next/link';
import { notFound } from 'next/navigation';

import { RevisionTimeline } from '@/components/revision-timeline';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { api, ApiError } from '@/lib/api';
import type { RevisionWithTriggers } from '@/lib/api';
import { formatConfidence, formatDateTime } from '@/lib/format';

export const dynamic = 'force-dynamic';

interface ChartPoint {
  ts: number;
  confidence: number;
  label: string;
  isSkeptic: boolean;
}

const CHART_W = 800;
const CHART_H = 220;
const CHART_PAD_X = 40;
const CHART_PAD_Y = 30;

function tsOf(value: string | undefined, fallback: number): number {
  if (!value) return fallback;
  const t = new Date(value).getTime();
  return Number.isNaN(t) ? fallback : t;
}

function buildChartPoints(
  revisions: RevisionWithTriggers[],
  initialConfidence: number,
  initialTimestamp: string | undefined,
): ChartPoint[] {
  const anchorTs = tsOf(initialTimestamp, Date.now());
  const sorted = [...revisions].sort(
    (a, b) =>
      tsOf(a.revision.revised_at, anchorTs) -
      tsOf(b.revision.revised_at, anchorTs),
  );
  const points: ChartPoint[] = [];
  // Anchor: belief's first appearance. previous_confidence on the first
  // revision is what the belief held before any revisions arrived.
  const firstPrev =
    sorted[0]?.revision.previous_confidence ?? initialConfidence;
  points.push({
    ts: anchorTs,
    confidence: firstPrev,
    label: 'initial',
    isSkeptic: false,
  });
  for (const r of sorted) {
    points.push({
      ts: tsOf(r.revision.revised_at, anchorTs),
      confidence: r.revision.new_confidence,
      label: r.revision.revised_by_agent,
      isSkeptic: r.revision.revised_by_agent === 'skeptic',
    });
  }
  return points;
}

function ConfidenceChart({ points }: { points: ChartPoint[] }) {
  if (points.length < 2) {
    return (
      <div className="rounded-lg border border-dashed border-border p-6 text-sm text-muted-foreground">
        Not enough revision history to chart yet — needs at least one revision.
      </div>
    );
  }
  const tsMin = points[0].ts;
  const tsMax = points[points.length - 1].ts;
  const range = Math.max(tsMax - tsMin, 1);
  const xAt = (ts: number) =>
    CHART_PAD_X + ((ts - tsMin) / range) * (CHART_W - 2 * CHART_PAD_X);
  const yAt = (c: number) => CHART_PAD_Y + (1 - c) * (CHART_H - 2 * CHART_PAD_Y);

  // Build a step path: vertical segment at each event so revisions
  // read as discrete jumps rather than misleading linear interpolation.
  const segs: string[] = [];
  for (let i = 0; i < points.length; i++) {
    const p = points[i];
    if (i === 0) {
      segs.push(`M ${xAt(p.ts)} ${yAt(p.confidence)}`);
      continue;
    }
    const prev = points[i - 1];
    segs.push(`L ${xAt(p.ts)} ${yAt(prev.confidence)}`);
    segs.push(`L ${xAt(p.ts)} ${yAt(p.confidence)}`);
  }
  const path = segs.join(' ');

  return (
    <div className="rounded-lg border border-border bg-card">
      <svg
        viewBox={`0 0 ${CHART_W} ${CHART_H}`}
        className="w-full"
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label="Belief confidence over time"
      >
        {[0, 0.25, 0.5, 0.75, 1].map((c) => (
          <g key={c}>
            <line
              x1={CHART_PAD_X}
              x2={CHART_W - CHART_PAD_X}
              y1={yAt(c)}
              y2={yAt(c)}
              stroke="currentColor"
              strokeOpacity="0.08"
            />
            <text
              x={CHART_PAD_X - 8}
              y={yAt(c) + 4}
              fontSize="10"
              textAnchor="end"
              className="fill-muted-foreground"
            >
              {Math.round(c * 100)}
            </text>
          </g>
        ))}
        <path d={path} fill="none" stroke="currentColor" strokeWidth="1.5" />
        {points.map((p, i) => (
          <g key={i}>
            <circle
              cx={xAt(p.ts)}
              cy={yAt(p.confidence)}
              r="5"
              className={
                p.isSkeptic
                  ? 'fill-destructive stroke-destructive'
                  : 'fill-primary stroke-primary'
              }
            />
            <title>
              {new Date(p.ts).toISOString()} · {p.label} · confidence{' '}
              {Math.round(p.confidence * 100)}%
            </title>
          </g>
        ))}
        <text
          x={CHART_PAD_X}
          y={CHART_H - 8}
          fontSize="10"
          className="fill-muted-foreground"
        >
          {formatDateTime(new Date(tsMin).toISOString())}
        </text>
        <text
          x={CHART_W - CHART_PAD_X}
          y={CHART_H - 8}
          fontSize="10"
          textAnchor="end"
          className="fill-muted-foreground"
        >
          {formatDateTime(new Date(tsMax).toISOString())}
        </text>
      </svg>
    </div>
  );
}

export default async function BeliefTimelinePage(props: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await props.params;
  let detail;
  try {
    detail = await api.belief(id);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) notFound();
    throw e;
  }
  const { belief, revisions } = detail;
  const points = buildChartPoints(revisions, belief.confidence, belief.last_revised_at);
  const skepticCount = revisions.filter(
    (r) => r.revision.revised_by_agent === 'skeptic',
  ).length;

  return (
    <main className="space-y-6">
      <header className="space-y-2">
        <div className="text-xs uppercase tracking-wide text-muted-foreground">
          <Link href={`/beliefs/${encodeURIComponent(id)}`} className="hover:underline">
            ← belief detail
          </Link>
        </div>
        <h1 className="text-2xl font-semibold leading-snug">{belief.statement}</h1>
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <Badge variant="secondary">{belief.topic}</Badge>
          <Badge variant="outline">
            current confidence {formatConfidence(belief.confidence)}
          </Badge>
          <span>· {revisions.length} revision(s)</span>
          {skepticCount > 0 ? (
            <Badge variant="destructive">
              {skepticCount} Skeptic challenge(s)
            </Badge>
          ) : null}
        </div>
      </header>

      <section className="space-y-2">
        <h2 className="text-base font-semibold tracking-tight">
          Confidence over time
        </h2>
        <p className="text-xs text-muted-foreground">
          Step-chart of confidence changes per revision. Skeptic challenges are
          marked in destructive color; non-Skeptic revisions in primary. Hover a
          point for the exact timestamp + agent.
        </p>
        <ConfidenceChart points={points} />
      </section>

      <section className="space-y-3">
        <h2 className="text-base font-semibold tracking-tight">
          Revision detail
        </h2>
        {revisions.length === 0 ? (
          <Card>
            <CardContent className="pt-6 text-sm text-muted-foreground">
              No revisions yet — this belief is in its initial state.
            </CardContent>
          </Card>
        ) : (
          <RevisionTimeline revisions={revisions} />
        )}
      </section>
    </main>
  );
}

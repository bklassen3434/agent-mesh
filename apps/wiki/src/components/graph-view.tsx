'use client';

import cytoscape, { type Core, type ElementDefinition } from 'cytoscape';
import coseBilkent from 'cytoscape-cose-bilkent';
import { X } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { useEffect, useMemo, useRef, useState } from 'react';

import { Button } from '@/components/ui/button';
import { Slider } from '@/components/ui/slider';
import type { GraphDataEdge, GraphDataNode } from '@/lib/api';
import { formatDateTime } from '@/lib/format';

// Register the force-directed layout once per module load.
let layoutRegistered = false;
if (!layoutRegistered) {
  cytoscape.use(coseBilkent);
  layoutRegistered = true;
}

// Spec palette: node color by entity type. Legend renders this order.
const TYPE_COLOR: Record<string, string> = {
  paper: '#3b82f6', // blue
  model: '#8b5cf6', // purple
  benchmark: '#fb7185', // coral
  lab: '#14b8a6', // teal
  person: '#f59e0b', // amber
  concept: '#94a3b8', // gray
  // extra entity types the mesh may emit
  method: '#10b981',
  repo: '#0ea5e9',
};
const LEGEND_TYPES = ['paper', 'model', 'benchmark', 'lab', 'person', 'concept'];

function colorFor(type: string): string {
  return TYPE_COLOR[type] ?? '#94a3b8';
}

function scale(value: number, lo: number, hi: number, outLo: number, outHi: number): number {
  if (hi <= lo) return outLo;
  const t = (value - lo) / (hi - lo);
  return outLo + Math.max(0, Math.min(1, t)) * (outHi - outLo);
}

type TooltipState = {
  x: number;
  y: number;
  label: string;
  type: string;
  beliefCount: number;
  lastClaimAt: string | null;
};

export function GraphView({
  nodes,
  edges,
  totalEntities,
}: {
  nodes: GraphDataNode[];
  edges: GraphDataEdge[];
  totalEntities: number;
}) {
  const router = useRouter();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cyRef = useRef<Core | null>(null);
  const searchRef = useRef('');

  const allTypes = useMemo(
    () => Array.from(new Set(nodes.map((n) => n.type))).sort(),
    [nodes],
  );
  const maxBelief = useMemo(
    () => Math.max(1, ...nodes.map((n) => n.belief_count)),
    [nodes],
  );
  const labelById = useMemo(
    () => Object.fromEntries(nodes.map((n) => [n.id, n.label])),
    [nodes],
  );

  const [enabledTypes, setEnabledTypes] = useState<Set<string>>(() => new Set(allTypes));
  const [minBelief, setMinBelief] = useState(1);
  const [search, setSearch] = useState('');
  const [tooltip, setTooltip] = useState<TooltipState | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<GraphDataEdge | null>(null);

  // Keep the type set in sync if the data's type universe changes.
  useEffect(() => {
    setEnabledTypes(new Set(allTypes));
  }, [allTypes]);

  function applyHighlight(cy: Core, query: string) {
    const q = query.trim().toLowerCase();
    cy.batch(() => {
      cy.nodes().forEach((n) => {
        const match = q === '' || (n.data('label') as string).toLowerCase().includes(q);
        n.removeClass('dim match');
        if (q !== '') n.addClass(match ? 'match' : 'dim');
      });
      cy.edges().forEach((e) => {
        e.removeClass('dim');
        if (q !== '') e.addClass('dim');
      });
    });
  }

  // Build / rebuild the graph when the data or structural filters change.
  useEffect(() => {
    if (!containerRef.current) return;

    const visibleNodes = nodes.filter(
      (n) => enabledTypes.has(n.type) && n.belief_count >= minBelief,
    );
    const visibleIds = new Set(visibleNodes.map((n) => n.id));
    const beliefs = visibleNodes.map((n) => n.belief_count);
    const loB = Math.min(...(beliefs.length ? beliefs : [0]));
    const hiB = Math.max(...(beliefs.length ? beliefs : [1]));
    const claims = edges.map((e) => e.claim_count);
    const loC = Math.min(...(claims.length ? claims : [0]));
    const hiC = Math.max(...(claims.length ? claims : [1]));

    const elements: ElementDefinition[] = [
      ...visibleNodes.map((n) => ({
        group: 'nodes' as const,
        data: {
          id: n.id,
          label: n.label,
          type: n.type,
          beliefCount: n.belief_count,
          lastClaimAt: n.last_claim_at ?? null,
          color: colorFor(n.type),
          // belief count → node diameter, 20px (min) … 48px (max)
          size: scale(n.belief_count, loB, hiB, 20, 48),
        },
      })),
      ...edges
        .filter((e) => visibleIds.has(e.source) && visibleIds.has(e.target))
        .map((e, i) => ({
          group: 'edges' as const,
          data: {
            id: `e${i}`,
            source: e.source,
            target: e.target,
            relationship_type: e.relationship_type,
            claim_count: e.claim_count,
            // claim count → stroke width, 1px … 4px
            width: scale(e.claim_count, loC, hiC, 1, 4),
          },
        })),
    ];

    const cy = cytoscape({
      container: containerRef.current,
      elements,
      layout: {
        name: 'cose-bilkent',
        animate: false,
        idealEdgeLength: 90,
        nodeRepulsion: 5000,
        padding: 30,
      } as cytoscape.LayoutOptions,
      style: [
        {
          selector: 'node',
          style: {
            'background-color': 'data(color)',
            width: 'data(size)',
            height: 'data(size)',
            label: 'data(label)',
            'font-size': '10px',
            'font-family': '-apple-system, BlinkMacSystemFont, sans-serif',
            color: '#1a1a1a',
            'text-valign': 'bottom',
            'text-halign': 'center',
            'text-margin-y': 4,
            'min-zoomed-font-size': 8,
            'border-width': 1,
            'border-color': '#ffffff',
          },
        },
        {
          selector: 'edge',
          style: {
            'curve-style': 'bezier',
            'target-arrow-shape': 'triangle',
            'line-color': '#cbd5e1',
            'target-arrow-color': '#cbd5e1',
            width: 'data(width)',
            opacity: 0.6,
          },
        },
        { selector: 'node.dim', style: { opacity: 0.12, 'text-opacity': 0.1 } },
        { selector: 'edge.dim', style: { opacity: 0.05 } },
        { selector: 'node.match', style: { 'border-width': 3, 'border-color': '#1e293b' } },
        { selector: ':selected', style: { 'border-width': 3, 'border-color': '#1e293b' } },
      ],
    });

    cy.on('tap', 'node', (evt) => {
      const id = evt.target.data('id') as string;
      router.push(`/knowledge/entities/${encodeURIComponent(id)}`);
    });
    cy.on('tap', 'edge', (evt) => {
      setSelectedEdge({
        source: evt.target.data('source') as string,
        target: evt.target.data('target') as string,
        relationship_type: evt.target.data('relationship_type') as string,
        claim_count: evt.target.data('claim_count') as number,
      });
    });
    cy.on('tap', (evt) => {
      if (evt.target === cy) setSelectedEdge(null);
    });
    cy.on('mouseover', 'node', (evt) => {
      const n = evt.target;
      const rp = n.renderedPosition();
      setTooltip({
        x: rp.x,
        y: rp.y,
        label: n.data('label') as string,
        type: n.data('type') as string,
        beliefCount: n.data('beliefCount') as number,
        lastClaimAt: (n.data('lastClaimAt') as string | null) ?? null,
      });
    });
    cy.on('mouseout', 'node', () => setTooltip(null));
    cy.on('pan zoom', () => setTooltip(null));

    cyRef.current = cy;
    applyHighlight(cy, searchRef.current);
    return () => {
      cy.destroy();
      cyRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes, edges, enabledTypes, minBelief, router]);

  // Search highlights without re-running the layout.
  useEffect(() => {
    searchRef.current = search;
    const cy = cyRef.current;
    if (cy) applyHighlight(cy, search);
  }, [search]);

  const toggleType = (t: string) =>
    setEnabledTypes((prev) => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t);
      else next.add(t);
      return next;
    });

  const capped = totalEntities > nodes.length;

  return (
    <div className="space-y-3">
      {capped && (
        <p className="text-xs text-muted-foreground">
          Showing top {nodes.length} of {totalEntities} entities by belief count.
        </p>
      )}

      {/* desktop-only graph */}
      <div className="hidden md:block">
        {nodes.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border p-12 text-center text-sm text-muted-foreground">
            No entities yet — populate the mesh to see the graph.
          </div>
        ) : (
          <div className="relative h-[680px] w-full overflow-hidden rounded-lg border border-border bg-card">
            <div ref={containerRef} className="h-full w-full" />

            {/* controls panel — top right */}
            <div className="absolute right-3 top-3 w-60 space-y-3 rounded-lg border border-border bg-background/80 p-3 text-sm shadow-sm backdrop-blur">
              <input
                type="search"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Escape') setSearch('');
                }}
                placeholder="Search nodes…"
                className="h-8 w-full rounded-md border border-input bg-background px-2 text-sm"
              />
              <div>
                <div className="mb-1 flex items-center justify-between text-xs text-muted-foreground">
                  <span>Min belief count</span>
                  <span className="font-mono tabular-nums">{minBelief}</span>
                </div>
                <Slider
                  min={1}
                  max={maxBelief}
                  step={1}
                  value={[minBelief]}
                  onValueChange={(v) => setMinBelief(v[0] ?? 1)}
                />
              </div>
              <div className="space-y-1">
                <div className="text-xs text-muted-foreground">Entity types</div>
                {allTypes.map((t) => (
                  <label key={t} className="flex cursor-pointer items-center gap-2 text-xs">
                    <input
                      type="checkbox"
                      checked={enabledTypes.has(t)}
                      onChange={() => toggleType(t)}
                    />
                    <span
                      className="h-2.5 w-2.5 rounded-full"
                      style={{ backgroundColor: colorFor(t) }}
                    />
                    {t}
                  </label>
                ))}
              </div>
              <Button
                size="sm"
                variant="outline"
                className="w-full"
                onClick={() => cyRef.current?.fit(undefined, 30)}
              >
                Reset view
              </Button>
            </div>

            {/* legend — bottom left */}
            <div className="absolute bottom-3 left-3 rounded-lg border border-border bg-background/80 p-2.5 text-xs shadow-sm backdrop-blur">
              <div className="mb-1 font-medium text-muted-foreground">Entity type</div>
              <div className="grid grid-cols-2 gap-x-3 gap-y-0.5">
                {LEGEND_TYPES.map((t) => (
                  <div key={t} className="flex items-center gap-1.5">
                    <span
                      className="h-2.5 w-2.5 rounded-full"
                      style={{ backgroundColor: colorFor(t) }}
                    />
                    {t}
                  </div>
                ))}
              </div>
            </div>

            {/* hover tooltip */}
            {tooltip && (
              <div
                className="pointer-events-none absolute z-10 max-w-56 rounded-md border border-border bg-popover px-2.5 py-1.5 text-xs shadow-md"
                style={{ left: tooltip.x + 12, top: tooltip.y + 12 }}
              >
                <div className="font-medium">{tooltip.label}</div>
                <div className="text-muted-foreground">
                  {tooltip.type} · {tooltip.beliefCount} belief
                  {tooltip.beliefCount === 1 ? '' : 's'}
                </div>
                <div className="text-muted-foreground">
                  last claim: {formatDateTime(tooltip.lastClaimAt)}
                </div>
              </div>
            )}

            {/* edge detail side panel */}
            {selectedEdge && (
              <div className="absolute right-3 top-3 z-20 w-64 rounded-lg border border-border bg-background/95 p-3 text-sm shadow-md backdrop-blur">
                <div className="flex items-start justify-between">
                  <div className="font-medium">Relationship</div>
                  <button
                    type="button"
                    aria-label="Close"
                    onClick={() => setSelectedEdge(null)}
                    className="text-muted-foreground hover:text-foreground"
                  >
                    <X className="h-4 w-4" />
                  </button>
                </div>
                <dl className="mt-2 space-y-1.5">
                  <div>
                    <dt className="text-xs text-muted-foreground">Type</dt>
                    <dd className="font-mono">{selectedEdge.relationship_type}</dd>
                  </div>
                  <div>
                    <dt className="text-xs text-muted-foreground">Supporting claims</dt>
                    <dd className="font-mono tabular-nums">{selectedEdge.claim_count}</dd>
                  </div>
                  <div>
                    <dt className="text-xs text-muted-foreground">From → To</dt>
                    <dd>
                      {labelById[selectedEdge.source] ?? selectedEdge.source}
                      {' → '}
                      {labelById[selectedEdge.target] ?? selectedEdge.target}
                    </dd>
                  </div>
                </dl>
              </div>
            )}
          </div>
        )}
      </div>

      {/* small screens */}
      <div className="rounded-lg border border-dashed border-border p-8 text-center text-sm text-muted-foreground md:hidden">
        The graph view is desktop-only. Open this page on a wider screen.
      </div>
    </div>
  );
}

'use client';

import cytoscape, { type Core, type ElementDefinition } from 'cytoscape';
import coseBilkent from 'cytoscape-cose-bilkent';
import { useEffect, useRef } from 'react';

import { Button } from '@/components/ui/button';
import type { AgentGraphEdge, AgentGraphNode } from '@/lib/api';

// Register the force-directed layout once per module load.
let layoutRegistered = false;
if (!layoutRegistered) {
  cytoscape.use(coseBilkent);
  layoutRegistered = true;
}

const COORDINATOR_COLOR = '#1e293b'; // slate — the dispatching hub

function scale(value: number, lo: number, hi: number, outLo: number, outHi: number): number {
  if (hi <= lo) return outLo;
  const t = (value - lo) / (hi - lo);
  return outLo + Math.max(0, Math.min(1, t)) * (outHi - outLo);
}

// error rate 0 → green, 1 → red (linear through amber)
function errorColor(rate: number): string {
  const r = Math.round(scale(rate, 0, 1, 16, 220));
  const g = Math.round(scale(rate, 0, 1, 185, 38));
  return `rgb(${r}, ${g}, 56)`;
}

export function AgentGraphView({
  nodes,
  edges,
  onSelectAgent,
}: {
  nodes: AgentGraphNode[];
  edges: AgentGraphEdge[];
  onSelectAgent: (agent: string) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cyRef = useRef<Core | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const counts = nodes.filter((n) => n.role === 'agent').map((n) => n.invocation_count);
    const lo = Math.min(...(counts.length ? counts : [0]));
    const hi = Math.max(...(counts.length ? counts : [1]));
    const callCounts = edges.map((e) => e.call_count);
    const loC = Math.min(...(callCounts.length ? callCounts : [0]));
    const hiC = Math.max(...(callCounts.length ? callCounts : [1]));

    const elements: ElementDefinition[] = [
      ...nodes.map((n) => ({
        group: 'nodes' as const,
        data: {
          id: n.id,
          label: n.label,
          role: n.role,
          color: n.role === 'coordinator' ? COORDINATOR_COLOR : errorColor(n.error_rate),
          size:
            n.role === 'coordinator'
              ? 52
              : scale(n.invocation_count, lo, hi, 24, 50),
        },
      })),
      ...edges.map((e, i) => ({
        group: 'edges' as const,
        data: {
          id: `e${i}`,
          source: e.source,
          target: e.target,
          width: scale(e.call_count, loC, hiC, 1.5, 5),
        },
      })),
    ];

    const cy = cytoscape({
      container: containerRef.current,
      elements,
      layout: {
        name: 'cose-bilkent',
        animate: false,
        idealEdgeLength: 120,
        nodeRepulsion: 7000,
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
            'font-size': '11px',
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
        { selector: ':selected', style: { 'border-width': 3, 'border-color': '#1e293b' } },
      ],
    });

    cy.on('tap', 'node', (evt) => {
      if ((evt.target.data('role') as string) === 'agent') {
        onSelectAgent(evt.target.data('id') as string);
      }
    });

    cyRef.current = cy;
    return () => {
      cy.destroy();
      cyRef.current = null;
    };
  }, [nodes, edges, onSelectAgent]);

  if (nodes.length <= 1) {
    return (
      <div className="rounded-lg border border-dashed border-border p-12 text-center text-sm text-muted-foreground">
        No agent activity yet — run the pipeline to populate the graph.
      </div>
    );
  }

  return (
    <div className="hidden md:block">
      <div className="relative h-[520px] w-full overflow-hidden rounded-lg border border-border bg-card">
        <div ref={containerRef} className="h-full w-full" />
        <div className="absolute bottom-3 left-3 rounded-lg border border-border bg-background/80 p-2.5 text-xs shadow-sm backdrop-blur">
          <div className="mb-1 font-medium text-muted-foreground">Node color</div>
          <div className="flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: errorColor(0) }} />
            no errors
            <span className="ml-2 h-2.5 w-2.5 rounded-full" style={{ backgroundColor: errorColor(1) }} />
            all errors
          </div>
          <div className="mt-1 text-muted-foreground">size = invocation volume</div>
        </div>
        <Button
          size="sm"
          variant="outline"
          className="absolute right-3 top-3"
          onClick={() => cyRef.current?.fit(undefined, 30)}
        >
          Reset view
        </Button>
      </div>
    </div>
  );
}

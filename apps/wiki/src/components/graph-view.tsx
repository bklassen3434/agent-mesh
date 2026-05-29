'use client';

import cytoscape, { type Core, type ElementDefinition } from 'cytoscape';
import { useRouter } from 'next/navigation';
import { useEffect, useMemo, useRef, useState } from 'react';

import type { GraphEdge, GraphNode } from '@/lib/api';

const TYPE_COLOR: Record<string, string> = {
  model: '#3b82f6',
  paper: '#8b5cf6',
  benchmark: '#10b981',
  method: '#f59e0b',
  person: '#ef4444',
  lab: '#ec4899',
  repo: '#0ea5e9',
  concept: '#64748b',
};

function colorFor(type: string): string {
  return TYPE_COLOR[type] ?? '#94a3b8';
}

export function GraphView({
  nodes,
  edges,
}: {
  nodes: GraphNode[];
  edges: GraphEdge[];
}) {
  const router = useRouter();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cyRef = useRef<Core | null>(null);
  const allTypes = useMemo(() => {
    const seen = new Set<string>();
    nodes.forEach((n) => seen.add(n.type));
    return Array.from(seen).sort();
  }, [nodes]);
  const [enabledTypes, setEnabledTypes] = useState<Set<string>>(
    () => new Set(allTypes),
  );

  useEffect(() => {
    if (!containerRef.current) return;
    const filteredNodeIds = new Set(
      nodes.filter((n) => enabledTypes.has(n.type)).map((n) => n.id),
    );
    const elements: ElementDefinition[] = [
      ...nodes
        .filter((n) => filteredNodeIds.has(n.id))
        .map((n) => ({
          group: 'nodes' as const,
          data: { id: n.id, label: n.label, type: n.type },
        })),
      ...edges
        .filter(
          (e) => filteredNodeIds.has(e.source) && filteredNodeIds.has(e.target),
        )
        .map((e) => ({
          group: 'edges' as const,
          data: {
            id: e.id,
            source: e.source,
            target: e.target,
            label: e.type,
          },
        })),
    ];

    const cy = cytoscape({
      container: containerRef.current,
      elements,
      layout: { name: 'cose', animate: false, padding: 30 },
      style: [
        {
          selector: 'node',
          style: {
            label: 'data(label)',
            'font-size': '11px',
            'font-family': '-apple-system, BlinkMacSystemFont, sans-serif',
            color: '#1a1a1a',
            'text-valign': 'bottom',
            'text-halign': 'center',
            'text-margin-y': 6,
            'background-color': (ele: cytoscape.NodeSingular) =>
              colorFor(ele.data('type') as string),
            'border-width': 1,
            'border-color': '#ffffff',
            width: 22,
            height: 22,
          },
        },
        {
          selector: 'edge',
          style: {
            'curve-style': 'bezier',
            'target-arrow-shape': 'triangle',
            'line-color': '#cbd5e1',
            'target-arrow-color': '#cbd5e1',
            width: 1.2,
            opacity: 0.7,
            label: 'data(label)',
            'font-size': '9px',
            color: '#64748b',
            'text-rotation': 'autorotate',
            'text-background-color': '#ffffff',
            'text-background-opacity': 1,
            'text-background-padding': '2px',
          },
        },
        {
          selector: 'node:selected',
          style: { 'border-width': 3, 'border-color': '#1e293b' },
        },
      ],
    });
    cy.on('tap', 'node', (evt) => {
      const id = evt.target.data('id') as string;
      router.push(`/knowledge/entities/${encodeURIComponent(id)}`);
    });
    cyRef.current = cy;
    return () => {
      cy.destroy();
      cyRef.current = null;
    };
  }, [nodes, edges, enabledTypes, router]);

  const toggleType = (t: string) => {
    setEnabledTypes((prev) => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t);
      else next.add(t);
      return next;
    });
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        {allTypes.map((t) => {
          const enabled = enabledTypes.has(t);
          return (
            <button
              key={t}
              type="button"
              onClick={() => toggleType(t)}
              className={
                'inline-flex items-center gap-2 rounded-md border px-2.5 py-1 text-xs font-mono transition-colors ' +
                (enabled
                  ? 'border-foreground/20 bg-background'
                  : 'border-border bg-muted text-muted-foreground line-through')
              }
            >
              <span
                className="h-2.5 w-2.5 rounded-full"
                style={{ backgroundColor: colorFor(t) }}
              />
              {t}
            </button>
          );
        })}
      </div>
      {nodes.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border p-12 text-center text-sm text-muted-foreground">
          No entities yet — populate the mesh via `make pipeline` to see the graph.
        </div>
      ) : (
        <div
          ref={containerRef}
          className="h-[640px] w-full rounded-lg border border-border bg-card"
        />
      )}
    </div>
  );
}

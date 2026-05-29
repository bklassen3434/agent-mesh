import { GraphView } from '@/components/graph-view';
import { api } from '@/lib/api';

export const dynamic = 'force-dynamic';

export default async function GraphPage() {
  const graph = await api.graphData();
  return (
    <main className="space-y-4">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Knowledge graph</h1>
        <p className="text-sm text-muted-foreground">
          Node size encodes belief count, edge thickness encodes supporting claims,
          color encodes entity type. Click a node to open its page, an edge for
          relationship detail.
        </p>
      </header>
      <GraphView
        nodes={graph.nodes}
        edges={graph.edges}
        totalEntities={graph.total_entities}
      />
    </main>
  );
}

import { GraphView } from '@/components/graph-view';
import { api } from '@/lib/api';

export const dynamic = 'force-dynamic';

export default async function GraphPage() {
  const graph = await api.graph({ max_nodes: 500, max_edges: 2000 });
  return (
    <main className="space-y-4">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Knowledge graph</h1>
        <p className="text-sm text-muted-foreground">
          {graph.nodes.length} entities · {graph.edges.length} relationships.
          Filter by entity type, click a node to open its page.
        </p>
      </header>
      <GraphView nodes={graph.nodes} edges={graph.edges} />
    </main>
  );
}

import { AgentsPanel } from '@/components/agents-panel';
import { api, type AgentGraph, type AgentRosterEntry } from '@/lib/api';

export const dynamic = 'force-dynamic';

export default async function AgentsPage({
  searchParams,
}: {
  searchParams: Promise<{ field?: string }>;
}) {
  const { field: fieldParam } = await searchParams;
  const field = fieldParam ?? 'ai-robotics';

  // Each source degrades on its own — a missing one must not blank the page.
  const [roster, graph] = await Promise.all([
    api.agentRoster(field).catch((): AgentRosterEntry[] => []),
    api
      .agentGraph(field)
      .catch((): AgentGraph => ({ nodes: [{ id: 'coordinator', label: 'coordinator', role: 'coordinator', invocation_count: 0, error_rate: 0 }], edges: [] })),
  ]);

  return (
    <main className="space-y-4">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Agents</h1>
        <p className="text-sm text-muted-foreground">
          What each agent is thinking. The coordinator dispatches every agent;
          node size encodes invocation volume, color encodes error rate. Click an
          agent to inspect its current memory and recent invocations, then drill
          into one invocation&apos;s inputs, outputs, and injected context.
        </p>
      </header>
      <AgentsPanel field={field} initialRoster={roster} initialGraph={graph} />
    </main>
  );
}

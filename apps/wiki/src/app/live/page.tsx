import { MeshPulse } from '@/components/mesh-pulse';
import { api, type AgentInvocation } from '@/lib/api';

export const dynamic = 'force-dynamic';

export default async function LivePage({
  searchParams,
}: {
  searchParams: Promise<{ field?: string }>;
}) {
  const { field: fieldParam } = await searchParams;
  const field = fieldParam ?? 'ai-robotics';

  // Seed with the most-recent activity so the board animates on first paint;
  // the client then polls the /activity firehose incrementally. Degrade to an
  // empty board rather than blanking the page if the API is unreachable.
  const initial = await api
    .agentActivity(field, undefined, 60)
    .catch((): AgentInvocation[] => []);

  return (
    <main className="space-y-4">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Live</h1>
        <p className="text-sm text-muted-foreground">
          The controller, working in real time. Every packet is one skill
          dispatch reacting to a tension — grabbing sources, extracting claims,
          synthesizing beliefs into the central knowledge base, then challenging
          and maintaining them. Colour shows whether the activation changed
          anything.
        </p>
      </header>
      <MeshPulse field={field} initial={initial} />
    </main>
  );
}

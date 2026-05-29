import { PipelinesPanel } from '@/components/pipelines-panel';
import { api, type PipelineRun, type Schedule, type SchedulerJobStatus } from '@/lib/api';

export const dynamic = 'force-dynamic';

export default async function PipelinesPage() {
  // Each source is independent and degrades on its own: no schedule store
  // (local/in-memory) or no running scheduler must not blank the page.
  const [schedules, status, runs] = await Promise.all([
    api.schedules().catch((): Schedule[] => []),
    api.schedulerStatus().catch((): SchedulerJobStatus[] => []),
    api.pipelineRuns(20).catch((): PipelineRun[] => []),
  ]);
  const langfuseUrl = process.env.NEXT_PUBLIC_LANGFUSE_URL ?? null;

  return (
    <main className="space-y-6">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Pipelines</h1>
        <p className="text-sm text-muted-foreground">
          Schedule the coordinator and skeptic sweep, trigger runs on demand, and
          review recent run history. Live state refreshes every 30 seconds.
        </p>
      </header>
      <PipelinesPanel
        initialSchedules={schedules}
        initialStatus={status}
        initialRuns={runs}
        langfuseUrl={langfuseUrl}
      />
    </main>
  );
}

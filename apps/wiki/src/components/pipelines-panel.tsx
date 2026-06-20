'use client';

import { ChevronDown, ChevronRight, ExternalLink, Play } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  ApiError,
  api,
  type PipelineRun,
  type Schedule,
  type SchedulerJobStatus,
} from '@/lib/api';
import { formatDateTime } from '@/lib/format';

const INTERVAL_OPTIONS = [1, 2, 4, 6, 12, 24, 48];

const PIPELINES: { jobId: string; name: string; description: string }[] = [
  {
    jobId: 'controller',
    name: 'Controller',
    description:
      'The deterministic orchestrator: senses the field into tensions and runs the whole loop ' +
      '— scout, extract, resolve, synthesize, challenge, investigate, and periodic belief/memory ' +
      'consolidation — under an explicit rule table, per round to quiescence.',
  },
];

function humanInterval(h: number): string {
  if (h === 1) return 'Every hour';
  if (h < 24) return `Every ${h} hours`;
  if (h === 24) return 'Every day';
  return `Every ${h / 24} days`;
}

function jobLabel(runType: string): string {
  return PIPELINES.find((p) => p.jobId === runType)?.name ?? runType;
}

function runStatus(run: PipelineRun): { label: string; tone: string } {
  if (!run.finished_at) return { label: 'running', tone: 'text-blue-600' };
  if ((run.errors?.length ?? 0) > 0) return { label: 'failed', tone: 'text-red-600' };
  return { label: 'completed', tone: 'text-emerald-700' };
}

function duration(run: PipelineRun): string {
  if (!run.started_at || !run.finished_at) return '—';
  const ms = new Date(run.finished_at).getTime() - new Date(run.started_at).getTime();
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

export function PipelinesPanel({
  initialSchedules,
  initialStatus,
  initialRuns,
  langfuseUrl,
}: {
  initialSchedules: Schedule[];
  initialStatus: SchedulerJobStatus[];
  initialRuns: PipelineRun[];
  langfuseUrl: string | null;
}) {
  const [schedules, setSchedules] = useState(initialSchedules);
  const [status, setStatus] = useState(initialStatus);
  const [runs, setRuns] = useState(initialRuns);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<Record<string, boolean>>({});

  const refresh = useCallback(async () => {
    const [s, st, r] = await Promise.all([
      api.schedules().catch(() => null),
      api.schedulerStatus().catch(() => null),
      api.pipelineRuns(20).catch(() => null),
    ]);
    if (s) setSchedules(s);
    if (st) setStatus(st);
    if (r) setRuns(r);
  }, []);

  useEffect(() => {
    const id = setInterval(refresh, 30_000);
    return () => clearInterval(id);
  }, [refresh]);

  const scheduleByJob = Object.fromEntries(schedules.map((s) => [s.job_id, s]));
  const statusByJob = Object.fromEntries(status.map((s) => [s.job_id, s]));
  const lastRunByJob = (jobId: string) => runs.find((r) => r.run_type === jobId);
  const isRunning = (jobId: string) =>
    busy[jobId] === true || statusByJob[jobId]?.state === 'running';

  async function changeInterval(jobId: string, hours: number) {
    const prev = schedules;
    setSchedules((cur) =>
      cur.map((s) => (s.job_id === jobId ? { ...s, interval_hours: hours } : s)),
    );
    setError(null);
    try {
      await api.updateSchedule(jobId, { interval_hours: hours });
      await refresh();
    } catch {
      setSchedules(prev);
      setError('Could not update interval — change reverted.');
    }
  }

  async function toggleEnabled(jobId: string, enabled: boolean) {
    const prev = schedules;
    setSchedules((cur) => cur.map((s) => (s.job_id === jobId ? { ...s, enabled } : s)));
    setError(null);
    try {
      await api.updateSchedule(jobId, { enabled });
      await refresh();
    } catch {
      setSchedules(prev);
      setError('Could not update — change reverted.');
    }
  }

  async function runNow(jobId: string) {
    setBusy((b) => ({ ...b, [jobId]: true }));
    setError(null);
    try {
      await api.triggerPipeline(jobId);
      await refresh();
    } catch (e) {
      const code = e instanceof ApiError ? e.status : 0;
      setError(code === 409 ? 'A run is already in progress.' : 'Could not trigger a run.');
    } finally {
      setBusy((b) => ({ ...b, [jobId]: false }));
    }
  }

  const storeDown = schedules.length === 0;

  return (
    <div className="space-y-8">
      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}
      {storeDown && (
        <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          Schedule store unavailable — configure LANGGRAPH_POSTGRES_URL to manage
          schedules. Run history below still reflects past runs.
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        {PIPELINES.map((p) => (
          <ScheduleCard
            key={p.jobId}
            meta={p}
            schedule={scheduleByJob[p.jobId]}
            status={statusByJob[p.jobId]}
            lastRun={lastRunByJob(p.jobId)}
            running={isRunning(p.jobId)}
            onInterval={(h) => changeInterval(p.jobId, h)}
            onToggle={(en) => toggleEnabled(p.jobId, en)}
            onRun={() => runNow(p.jobId)}
          />
        ))}
      </div>

      <RunHistory runs={runs} langfuseUrl={langfuseUrl} />
    </div>
  );
}

function StatusDot({ enabled }: { enabled: boolean }) {
  return (
    <span
      className={
        'inline-block h-2.5 w-2.5 rounded-full ' +
        (enabled ? 'bg-emerald-500' : 'bg-muted-foreground/50')
      }
    />
  );
}

function ScheduleCard({
  meta,
  schedule,
  status,
  lastRun,
  running,
  onInterval,
  onToggle,
  onRun,
}: {
  meta: { jobId: string; name: string; description: string };
  schedule: Schedule | undefined;
  status: SchedulerJobStatus | undefined;
  lastRun: PipelineRun | undefined;
  running: boolean;
  onInterval: (hours: number) => void;
  onToggle: (enabled: boolean) => void;
  onRun: () => void;
}) {
  const enabled = schedule?.enabled ?? false;
  const interval = schedule?.interval_hours ?? 6;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle className="text-base">{meta.name}</CardTitle>
            <p className="mt-1 text-xs text-muted-foreground">{meta.description}</p>
          </div>
          <div className="flex items-center gap-2 text-xs">
            <StatusDot enabled={enabled} />
            <span className={enabled ? 'text-emerald-700' : 'text-muted-foreground'}>
              {enabled ? 'enabled' : 'disabled'}
            </span>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
          <div>
            <dt className="text-xs text-muted-foreground">Interval</dt>
            <dd>{humanInterval(interval)}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted-foreground">Next run</dt>
            <dd>
              {!enabled
                ? 'disabled'
                : status?.next_run_at
                  ? formatDateTime(status.next_run_at)
                  : '—'}
            </dd>
          </div>
          <div className="col-span-2">
            <dt className="text-xs text-muted-foreground">Last run</dt>
            <dd>
              {lastRun ? (
                <span>
                  {formatDateTime(lastRun.started_at)} ·{' '}
                  <span className={runStatus(lastRun).tone}>{runStatus(lastRun).label}</span>
                </span>
              ) : (
                <span className="text-muted-foreground">never</span>
              )}
            </dd>
          </div>
        </dl>

        <div className="flex flex-wrap items-center gap-4 border-t border-border pt-4">
          <div className="flex flex-col gap-1">
            <label className="text-xs text-muted-foreground">Interval</label>
            <Select
              value={String(interval)}
              onValueChange={(v) => onInterval(Number(v))}
              disabled={!schedule}
            >
              <SelectTrigger className="h-8 w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {INTERVAL_OPTIONS.map((h) => (
                  <SelectItem key={h} value={String(h)}>
                    {humanInterval(h)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-xs text-muted-foreground">Enabled</label>
            <div className="flex h-8 items-center">
              <Switch
                checked={enabled}
                onCheckedChange={onToggle}
                disabled={!schedule}
                aria-label={`${meta.name} enabled`}
              />
            </div>
          </div>
          <div className="ml-auto flex flex-col gap-1">
            <label className="text-xs text-transparent">Run</label>
            <Button size="sm" variant="outline" onClick={onRun} disabled={running}>
              <Play className="mr-1.5 h-3.5 w-3.5" />
              {running ? 'Running…' : 'Run now'}
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function RunHistory({
  runs,
  langfuseUrl,
}: {
  runs: PipelineRun[];
  langfuseUrl: string | null;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);

  if (runs.length === 0) {
    return (
      <section className="space-y-3">
        <h2 className="text-lg font-semibold tracking-tight">Run history</h2>
        <div className="rounded-lg border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
          No runs yet.
        </div>
      </section>
    );
  }

  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold tracking-tight">Run history</h2>
      <div className="rounded-lg border border-border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-6" />
              <TableHead>Pipeline</TableHead>
              <TableHead>Triggered by</TableHead>
              <TableHead>Started</TableHead>
              <TableHead>Duration</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="text-right">Claims</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {runs.map((run) => {
              const id = run.id ?? '';
              const open = expanded === id;
              const st = runStatus(run);
              return (
                <RunRow
                  key={id}
                  run={run}
                  open={open}
                  statusLabel={st.label}
                  statusTone={st.tone}
                  onToggle={() => setExpanded(open ? null : id)}
                  langfuseUrl={langfuseUrl}
                />
              );
            })}
          </TableBody>
        </Table>
      </div>
    </section>
  );
}

function RunRow({
  run,
  open,
  statusLabel,
  statusTone,
  onToggle,
  langfuseUrl,
}: {
  run: PipelineRun;
  open: boolean;
  statusLabel: string;
  statusTone: string;
  onToggle: () => void;
  langfuseUrl: string | null;
}) {
  const isCoordinator = run.run_type === 'ingest';
  return (
    <>
      <TableRow className="cursor-pointer" onClick={onToggle}>
        <TableCell className="text-muted-foreground">
          {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
        </TableCell>
        <TableCell className="font-medium">{jobLabel(run.run_type)}</TableCell>
        <TableCell>
          <Badge variant="outline">{run.triggered_by}</Badge>
        </TableCell>
        <TableCell className="whitespace-nowrap">{formatDateTime(run.started_at)}</TableCell>
        <TableCell className="tabular-nums">{duration(run)}</TableCell>
        <TableCell className={statusTone}>{statusLabel}</TableCell>
        <TableCell className="text-right tabular-nums">
          {isCoordinator ? run.claims_inserted : '—'}
        </TableCell>
      </TableRow>
      {open && (
        <TableRow className="bg-muted/30 hover:bg-muted/30">
          <TableCell />
          <TableCell colSpan={6}>
            <RunDetail run={run} langfuseUrl={langfuseUrl} />
          </TableCell>
        </TableRow>
      )}
    </>
  );
}

function RunDetail({ run, langfuseUrl }: { run: PipelineRun; langfuseUrl: string | null }) {
  const stages: { label: string; value: number }[] = [
    { label: 'papers scouted', value: run.papers_scouted },
    { label: 'sources', value: run.sources_inserted },
    { label: 'claims', value: run.claims_inserted },
    { label: 'entities', value: run.entities_created },
    { label: 'beliefs created', value: run.beliefs_created },
    { label: 'beliefs revised', value: run.beliefs_revised },
  ];
  const active = stages.filter((s) => s.value > 0);
  const errors = run.errors ?? [];

  return (
    <div className="space-y-3 py-1 text-sm">
      <div>
        <div className="text-xs uppercase tracking-wide text-muted-foreground">
          Agents / stages
        </div>
        <div className="mt-1 flex flex-wrap gap-2">
          {active.length === 0 ? (
            <span className="text-muted-foreground">No stage produced output.</span>
          ) : (
            active.map((s) => (
              <Badge key={s.label} variant="secondary" className="font-mono">
                {s.label}: {s.value}
              </Badge>
            ))
          )}
          {run.avg_extraction_latency_ms > 0 && (
            <Badge variant="outline" className="font-mono">
              avg extract {run.avg_extraction_latency_ms}ms
            </Badge>
          )}
        </div>
      </div>

      <div>
        <div className="text-xs uppercase tracking-wide text-muted-foreground">
          Errors ({errors.length})
        </div>
        {errors.length === 0 ? (
          <div className="mt-1 text-muted-foreground">No errors recorded.</div>
        ) : (
          <ul className="mt-1 space-y-1">
            {errors.slice(0, 10).map((e, i) => (
              <li key={i} className="font-mono text-xs">
                <span className="text-red-600">{e.error_type}</span> · {e.error_message}
                {e.paper_id ? <span className="text-muted-foreground"> · {e.paper_id}</span> : null}
              </li>
            ))}
          </ul>
        )}
      </div>

      {langfuseUrl && (
        <a
          href={langfuseUrl}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 text-xs text-[hsl(222_47%_30%)] hover:underline"
        >
          open Langfuse <ExternalLink className="h-3 w-3" />
        </a>
      )}
    </div>
  );
}

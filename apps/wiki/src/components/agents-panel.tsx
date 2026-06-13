'use client';

import { ExternalLink, X } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';

import { AgentGraphView } from '@/components/agent-graph-view';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  api,
  type AgentGraph,
  type AgentInvocation,
  type AgentInvocationDetail,
  type AgentMemory,
  type AgentRosterEntry,
} from '@/lib/api';
import { formatDateTime, formatNumber } from '@/lib/format';

function pct(rate: number): string {
  return `${Math.round(rate * 100)}%`;
}

function summaryText(s: Record<string, unknown> | null | undefined): string {
  if (!s) return '—';
  const preview = s['preview'];
  return typeof preview === 'string' ? preview : JSON.stringify(s);
}

export function AgentsPanel({
  field,
  initialRoster,
  initialGraph,
}: {
  field: string;
  initialRoster: AgentRosterEntry[];
  initialGraph: AgentGraph;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const [memory, setMemory] = useState<AgentMemory | null>(null);
  const [invocations, setInvocations] = useState<AgentInvocation[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    Promise.all([
      api.agentMemory(selected, field).catch(() => null),
      api.agentInvocations(selected, field).catch(() => null),
    ])
      .then(([m, inv]) => {
        if (cancelled) return;
        if (m === null && inv === null) {
          setError('Could not load agent detail.');
        }
        setMemory(m);
        setInvocations(inv ?? []);
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [selected, field]);

  return (
    <div className="space-y-6">
      <AgentGraphView
        nodes={initialGraph.nodes}
        edges={initialGraph.edges}
        onSelectAgent={setSelected}
      />

      <div className="grid gap-6 lg:grid-cols-[1fr_1.2fr]">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Agents</CardTitle>
          </CardHeader>
          <CardContent>
            {initialRoster.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No agent activity yet — run the pipeline to populate this view.
              </p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Agent</TableHead>
                    <TableHead className="text-right">Calls</TableHead>
                    <TableHead className="text-right">Errors</TableHead>
                    <TableHead className="text-right">Avg ms</TableHead>
                    <TableHead className="text-right">Cost</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {initialRoster.map((r) => (
                    <TableRow
                      key={r.agent}
                      data-selected={selected === r.agent}
                      className="cursor-pointer data-[selected=true]:bg-accent"
                    >
                      <TableCell>
                        <button
                          type="button"
                          className="font-medium hover:underline"
                          onClick={() => setSelected(r.agent)}
                        >
                          {r.agent}
                        </button>
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {formatNumber(r.invocations)}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {r.errors > 0 ? (
                          <span className="text-destructive">{pct(r.error_rate)}</span>
                        ) : (
                          '0%'
                        )}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {Math.round(r.avg_latency_ms)}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        ${r.total_cost_usd.toFixed(4)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>

        {selected ? (
          <AgentDetail
            agent={selected}
            memory={memory}
            invocations={invocations}
            loading={loading}
            error={error}
            onClose={() => setSelected(null)}
          />
        ) : (
          <Card>
            <CardContent className="flex h-full items-center justify-center p-12 text-sm text-muted-foreground">
              Select an agent — from the graph or the table — to inspect its memory
              and recent invocations.
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}

function AgentDetail({
  agent,
  memory,
  invocations,
  loading,
  error,
  onClose,
}: {
  agent: string;
  memory: AgentMemory | null;
  invocations: AgentInvocation[];
  loading: boolean;
  error: string | null;
  onClose: () => void;
}) {
  return (
    <Card data-testid="agent-detail">
      <CardHeader className="flex flex-row items-start justify-between space-y-0">
        <CardTitle className="text-base">{agent}</CardTitle>
        <button
          type="button"
          aria-label="Close"
          onClick={onClose}
          className="text-muted-foreground hover:text-foreground"
        >
          <X className="h-4 w-4" />
        </button>
      </CardHeader>
      <CardContent className="space-y-5">
        {loading && <p className="text-sm text-muted-foreground">Loading…</p>}
        {error && <p className="text-sm text-destructive">{error}</p>}

        {/* current memory */}
        <section>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Current memory
          </h3>
          {memory && memory.heuristics.length > 0 ? (
            <ul className="space-y-1 text-sm">
              {memory.heuristics.map((h) => (
                <li key={h.id} className="flex gap-2">
                  <Badge variant="secondary">{h.confidence.toFixed(2)}</Badge>
                  <span>{h.heuristic}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground">No active heuristics.</p>
          )}
          {memory && memory.episodic.length > 0 && (
            <ul className="mt-2 space-y-0.5 text-xs text-muted-foreground">
              {memory.episodic.slice(0, 8).map((e, i) => {
                const label = (e['outcome'] as { label?: string } | undefined)?.label;
                const summary = e['action_summary'] as string | undefined;
                return (
                  <li key={i}>
                    {label && <span className="font-mono">[{label}]</span>} {summary}
                  </li>
                );
              })}
            </ul>
          )}
        </section>

        {/* recent invocations */}
        <section>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Recent invocations
          </h3>
          {invocations.length === 0 && !loading ? (
            <p className="text-sm text-muted-foreground">No invocations recorded.</p>
          ) : (
            <ul className="space-y-1">
              {invocations.map((inv) => (
                <InvocationRow key={inv.id} inv={inv} />
              ))}
            </ul>
          )}
        </section>
      </CardContent>
    </Card>
  );
}

function InvocationRow({ inv }: { inv: AgentInvocation }) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<AgentInvocationDetail | null>(null);
  const [loading, setLoading] = useState(false);

  const toggle = useCallback(() => {
    const next = !open;
    setOpen(next);
    if (next && detail === null && inv.id) {
      const id = inv.id;
      setLoading(true);
      api
        .agentInvocation(id)
        .then(setDetail)
        .catch(() => setDetail(null))
        .finally(() => setLoading(false));
    }
  }, [open, detail, inv.id]);

  return (
    <li className="rounded-md border border-border">
      <button
        type="button"
        onClick={toggle}
        data-testid="invocation-row"
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm hover:bg-accent"
      >
        <span className="font-mono">{inv.skill}</span>
        <span className="flex items-center gap-2">
          <Badge variant={inv.status === 'ok' ? 'secondary' : 'destructive'}>
            {inv.status}
          </Badge>
          <span className="text-xs text-muted-foreground">
            {formatDateTime(inv.created_at)}
          </span>
        </span>
      </button>
      {open && (
        <div className="space-y-3 border-t border-border px-3 py-2 text-xs" data-testid="invocation-detail">
          {loading && <p className="text-muted-foreground">Loading…</p>}
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-muted-foreground">
            {inv.model && <span>model: {inv.model}</span>}
            {inv.latency_ms != null && <span>{inv.latency_ms} ms</span>}
            {inv.input_tokens != null && <span>in: {inv.input_tokens} tok</span>}
            {inv.output_tokens != null && <span>out: {inv.output_tokens} tok</span>}
            {inv.cost_usd != null && <span>${inv.cost_usd.toFixed(5)}</span>}
          </div>
          {inv.error_message && (
            <p className="text-destructive">
              {inv.error_type}: {inv.error_message}
            </p>
          )}
          <Field label="Input">{summaryText(inv.input_summary)}</Field>
          <Field label="Output">{summaryText(inv.output_summary)}</Field>
          {inv.memory_block && <Field label="Injected memory">{inv.memory_block}</Field>}
          {detail && detail.applied_heuristics.length > 0 && (
            <div>
              <div className="mb-1 font-semibold uppercase tracking-wide text-muted-foreground">
                Applied heuristics
              </div>
              <ul className="space-y-0.5">
                {detail.applied_heuristics.map((h) => (
                  <li key={h.id}>
                    ({h.confidence.toFixed(2)}) {h.heuristic}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {detail?.langfuse_url && (
            <a
              href={detail.langfuse_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-primary hover:underline"
            >
              View trace in Langfuse <ExternalLink className="h-3 w-3" />
            </a>
          )}
        </div>
      )}
    </li>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-0.5 font-semibold uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded bg-muted p-2 font-mono text-[11px]">
        {children}
      </pre>
    </div>
  );
}

import Link from 'next/link';

import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import type { RevisionWithTriggers } from '@/lib/api';
import { formatConfidence, formatDateTime } from '@/lib/format';

export function RevisionTimeline({ revisions }: { revisions: RevisionWithTriggers[] }) {
  if (revisions.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-border p-6 text-sm text-muted-foreground">
        No revisions yet — this belief is in its initial state.
      </div>
    );
  }

  return (
    <ol className="relative space-y-6 border-l-2 border-border pl-6">
      {revisions.map((r) => {
        const delta = r.revision.new_confidence - r.revision.previous_confidence;
        const deltaSign = delta > 0 ? '+' : '';
        return (
          <li key={r.revision.id} className="relative">
            <span className="absolute -left-[34px] flex h-5 w-5 items-center justify-center rounded-full border-2 border-primary bg-background">
              <span className="h-1.5 w-1.5 rounded-full bg-primary" />
            </span>
            <Card>
              <CardContent className="space-y-3 pt-6">
                <div className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    <span>
                      {formatDateTime(r.revision.revised_at)} · by{' '}
                      <span className="font-mono">{r.revision.revised_by_agent}</span>
                    </span>
                    {r.revision.revised_by_agent === 'skeptic' ? (
                      <Badge variant="destructive">Skeptic challenge</Badge>
                    ) : null}
                  </div>
                  <Badge variant={delta >= 0 ? 'secondary' : 'destructive'}>
                    confidence {formatConfidence(r.revision.previous_confidence)} →{' '}
                    {formatConfidence(r.revision.new_confidence)} ({deltaSign}
                    {(delta * 100).toFixed(0)}%)
                  </Badge>
                </div>

                <div className="grid gap-3 md:grid-cols-2">
                  <div>
                    <div className="text-xs uppercase tracking-wide text-muted-foreground">Previous</div>
                    <p className="mt-1 text-sm">{r.revision.previous_statement}</p>
                  </div>
                  <div>
                    <div className="text-xs uppercase tracking-wide text-muted-foreground">New</div>
                    <p className="mt-1 text-sm font-medium">{r.revision.new_statement}</p>
                  </div>
                </div>

                {r.revision.rationale ? (
                  <div>
                    <div className="text-xs uppercase tracking-wide text-muted-foreground">Rationale</div>
                    <p className="mt-1 text-sm text-muted-foreground">{r.revision.rationale}</p>
                  </div>
                ) : null}

                {r.trigger_claims.length > 0 ? (
                  <div>
                    <div className="text-xs uppercase tracking-wide text-muted-foreground">Triggered by</div>
                    <div className="mt-2 flex flex-wrap gap-2">
                      {r.trigger_claims.map((c) => {
                        const cid = c.id!;
                        return (
                          <Link
                            key={cid}
                            href={`/knowledge/claims/${cid}`}
                            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-accent/40 px-2 py-1 text-xs hover:bg-accent"
                          >
                            <span className="font-mono">{c.predicate}</span>
                            <span className="text-muted-foreground">·</span>
                            <span className="text-muted-foreground">{cid.slice(0, 8)}</span>
                          </Link>
                        );
                      })}
                    </div>
                  </div>
                ) : null}
              </CardContent>
            </Card>
          </li>
        );
      })}
    </ol>
  );
}

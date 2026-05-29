import Link from 'next/link';

import { EmptyState } from '@/components/empty-state';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { api } from '@/lib/api';
import { formatConfidence, formatDateTime } from '@/lib/format';

export const dynamic = 'force-dynamic';

export default async function SkepticPage() {
  const activity = await api.skepticRecent(25);

  return (
    <main className="space-y-6">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">Skeptic activity</h1>
        <p className="text-sm text-muted-foreground">
          Belief revisions emitted by the Skeptic agent during out-of-band falsification
          sweeps. Each entry links to the belief being challenged and the counter-claims
          the skeptic introduced.
        </p>
      </header>

      {activity.length === 0 ? (
        <EmptyState
          title="No skeptic activity yet"
          description={
            <>
              Run <span className="font-mono">make skeptic</span> to dispatch a
              falsification sweep. The Curator picks beliefs worth challenging and the
              Skeptic emits counter-claims, which land here.
            </>
          }
        />
      ) : (
        <ol className="space-y-4">
          {activity.map((item) => {
            const delta = item.revision.new_confidence - item.revision.previous_confidence;
            const deltaSign = delta > 0 ? '+' : '';
            return (
              <li key={item.revision.id}>
                <Card>
                  <CardContent className="space-y-3 pt-6">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex items-center gap-2 text-xs text-muted-foreground">
                        <span>{formatDateTime(item.revision.revised_at)}</span>
                        <Badge variant="destructive">Skeptic challenge</Badge>
                      </div>
                      <Badge variant={delta >= 0 ? 'secondary' : 'destructive'}>
                        confidence {formatConfidence(item.revision.previous_confidence)} →{' '}
                        {formatConfidence(item.revision.new_confidence)} ({deltaSign}
                        {(delta * 100).toFixed(0)}%)
                      </Badge>
                    </div>

                    <div>
                      <div className="text-xs uppercase tracking-wide text-muted-foreground">
                        Belief
                      </div>
                      <Link
                        href={`/knowledge/beliefs/${item.belief.id}`}
                        className="mt-1 block font-medium hover:underline"
                      >
                        {item.belief.statement}
                      </Link>
                      <div className="mt-1 text-xs text-muted-foreground">
                        {item.belief.topic}
                      </div>
                    </div>

                    {item.revision.rationale ? (
                      <div>
                        <div className="text-xs uppercase tracking-wide text-muted-foreground">
                          Rationale
                        </div>
                        <p className="mt-1 text-sm text-muted-foreground">
                          {item.revision.rationale}
                        </p>
                      </div>
                    ) : null}

                    {item.trigger_claims.length > 0 ? (
                      <div>
                        <div className="text-xs uppercase tracking-wide text-muted-foreground">
                          Counter-claims
                        </div>
                        <div className="mt-2 flex flex-wrap gap-2">
                          {item.trigger_claims.map((c) => {
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
      )}
    </main>
  );
}

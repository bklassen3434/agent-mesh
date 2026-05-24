import Link from 'next/link';

import { EmptyState } from '@/components/empty-state';
import { Pagination } from '@/components/pagination';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { api } from '@/lib/api';
import { formatConfidence, formatDateTime } from '@/lib/format';

export const dynamic = 'force-dynamic';

type SP = { [k: string]: string | string[] | undefined };

function pick(sp: SP, key: string): string | undefined {
  const v = sp[key];
  return Array.isArray(v) ? v[0] : v;
}

export default async function BeliefsPage(props: { searchParams: Promise<SP> }) {
  const sp = await props.searchParams;
  const topic = pick(sp, 'topic');
  const limit = Number(pick(sp, 'limit') ?? 50);
  const offset = Number(pick(sp, 'offset') ?? 0);

  const page = await api.listBeliefs({ topic, limit, offset });

  return (
    <main className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Beliefs</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Mutable, synthesised positions over the underlying claims. Click in to see
          the full provenance trail and revision timeline.
        </p>
      </div>

      <form className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <label htmlFor="topic" className="text-xs text-muted-foreground">
            Topic contains
          </label>
          <input
            id="topic"
            name="topic"
            defaultValue={topic ?? ''}
            className="h-9 rounded-md border border-input bg-background px-3 text-sm"
            placeholder="e.g. SOTA"
          />
        </div>
        <button
          type="submit"
          className="h-9 rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground hover:bg-primary/90"
        >
          Filter
        </button>
      </form>

      {page.items.length === 0 ? (
        <EmptyState
          title="No beliefs match"
          description={topic ? 'Try clearing the filter.' : 'Run the pipeline to synthesise beliefs.'}
        />
      ) : (
        <>
          <div className="space-y-3">
            {page.items.map((b) => (
              <Link key={b.id} href={`/beliefs/${b.id}`} className="block">
                <Card className="transition-colors hover:bg-accent/30">
                  <CardContent className="pt-6">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="text-xs uppercase tracking-wide text-muted-foreground">{b.topic}</div>
                        <p className="mt-1 font-medium">{b.statement}</p>
                      </div>
                      <div className="flex flex-col items-end gap-1 text-xs">
                        <Badge variant={b.is_currently_held ? 'secondary' : 'outline'}>
                          {b.is_currently_held ? 'held' : 'inactive'}
                        </Badge>
                        <span>{formatConfidence(b.confidence)}</span>
                        <span className="text-muted-foreground">
                          rev {b.revision_count} · {formatDateTime(b.last_revised_at)}
                        </span>
                      </div>
                    </div>
                  </CardContent>
                </Card>
              </Link>
            ))}
          </div>

          <Pagination
            total={page.total}
            limit={page.limit}
            offset={page.offset}
            basePath="/beliefs"
            searchParams={{ topic }}
          />
        </>
      )}
    </main>
  );
}

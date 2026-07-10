import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { api, type FieldOverview } from '@/lib/api';
import { getField } from '@/lib/auth-server';
import { formatConfidence, formatDateTime } from '@/lib/format';

export const dynamic = 'force-dynamic';

type OverviewBelief = FieldOverview['strongest'][number];

function BeliefRow({ belief, right }: { belief: OverviewBelief; right?: string }) {
  return (
    <li className="flex items-start justify-between gap-4 py-2">
      <div className="min-w-0">
        <p className="text-sm leading-snug">{belief.statement}</p>
        <p className="mt-0.5 text-xs text-muted-foreground">{belief.topic}</p>
      </div>
      <Badge variant="secondary" className="shrink-0 tabular-nums">
        {right ?? formatConfidence(belief.confidence)}
      </Badge>
    </li>
  );
}

function StatTile({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border p-4">
      <p className="text-2xl font-semibold tabular-nums">{value}</p>
      <p className="text-xs text-muted-foreground">{label}</p>
    </div>
  );
}

export default async function OverviewPage() {
  const field = await getField();
  const data = await api.fieldOverview(field);
  const days = data.movement.window_days;

  return (
    <main className="space-y-6">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Field overview</h1>
        <p className="text-sm text-muted-foreground">
          What this knowledge base currently holds about <span className="font-medium">{data.field}</span> — and where it is uncertain.
        </p>
      </header>

      <Card>
        <CardContent className="pt-6">
          {data.brief ? (
            <>
              <p className="whitespace-pre-line leading-relaxed">{data.brief.narrative}</p>
              <p className="mt-3 text-xs text-muted-foreground">
                Written {formatDateTime(data.brief.generated_at)} from {data.movement.held_total} held beliefs.
              </p>
            </>
          ) : (
            <p className="text-sm text-muted-foreground">
              No narrative brief yet — the controller writes one on its next maintenance pass.
            </p>
          )}
        </CardContent>
      </Card>

      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <StatTile label="beliefs held" value={data.movement.held_total} />
        <StatTile label={`new (last ${days}d)`} value={data.movement.new} />
        <StatTile label={`revised (last ${days}d)`} value={data.movement.revised} />
        <StatTile label={`dropped (last ${days}d)`} value={data.movement.dropped} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Strongest beliefs</CardTitle>
          </CardHeader>
          <CardContent>
            {data.strongest.length ? (
              <ul className="divide-y">
                {data.strongest.map((b) => (
                  <BeliefRow key={b.id} belief={b} />
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">Nothing held yet.</p>
            )}
          </CardContent>
        </Card>

        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Movement</CardTitle>
            </CardHeader>
            <CardContent>
              {data.recently_revised.length ? (
                <ul className="divide-y">
                  {data.recently_revised.map((b) => (
                    <BeliefRow key={b.id} belief={b} />
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-muted-foreground">No revisions in the last {days} days.</p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Contested</CardTitle>
            </CardHeader>
            <CardContent>
              {data.contested.length ? (
                <ul className="divide-y">
                  {data.contested.map((b) => (
                    <BeliefRow
                      key={b.id}
                      belief={b}
                      right={`${b.contradiction_count} counter`}
                    />
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-muted-foreground">No held belief is under challenge.</p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Open questions</CardTitle>
            </CardHeader>
            <CardContent>
              {data.gaps.length ? (
                <ul className="divide-y">
                  {data.gaps.map((g) => (
                    <li key={g.id} className="py-2">
                      <p className="text-sm leading-snug">{g.question}</p>
                      {g.rationale ? (
                        <p className="mt-0.5 text-xs text-muted-foreground">{g.rationale}</p>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-muted-foreground">No open investigations.</p>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </main>
  );
}

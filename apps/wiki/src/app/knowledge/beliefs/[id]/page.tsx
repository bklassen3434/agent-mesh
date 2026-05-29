import Link from 'next/link';
import { notFound } from 'next/navigation';

import { BeliefSignalsCard } from '@/components/belief-signals-card';
import { ClaimCard } from '@/components/claim-card';
import { RevisionTimeline } from '@/components/revision-timeline';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { api, ApiError } from '@/lib/api';
import { formatConfidence, formatDateTime } from '@/lib/format';

export const dynamic = 'force-dynamic';

export default async function BeliefDetailPage(props: { params: Promise<{ id: string }> }) {
  const { id } = await props.params;
  let detail;
  try {
    detail = await api.belief(id);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) notFound();
    throw e;
  }

  const { belief, supporting_claims, contradicting_claims, revisions, signals } = detail;

  return (
    <main className="space-y-8">
      <header className="space-y-2">
        <div className="text-xs uppercase tracking-wide text-muted-foreground">{belief.topic}</div>
        <h1 className="text-2xl font-semibold leading-snug">{belief.statement}</h1>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <Badge variant={belief.is_currently_held ? 'secondary' : 'outline'}>
            {belief.is_currently_held ? 'currently held' : 'no longer held'}
          </Badge>
          <Badge variant="secondary">{formatConfidence(belief.confidence)}</Badge>
          <span className="text-muted-foreground">
            revision {belief.revision_count} · last revised {formatDateTime(belief.last_revised_at)}
          </span>
        </div>
      </header>

      {signals ? <BeliefSignalsCard signals={signals} /> : null}

      <section className="grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Supporting claims ({supporting_claims.length})</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {supporting_claims.length === 0 ? (
              <div className="text-sm text-muted-foreground">None.</div>
            ) : (
              supporting_claims.map((c) => <ClaimCard key={c.claim.id} entry={c} />)
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Contradicting claims ({contradicting_claims.length})</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            {contradicting_claims.length === 0 ? (
              <div className="text-sm text-muted-foreground">None.</div>
            ) : (
              contradicting_claims.map((c) => <ClaimCard key={c.claim.id} entry={c} />)
            )}
          </CardContent>
        </Card>
      </section>

      <section>
        <div className="mb-4 flex items-baseline justify-between">
          <h2 className="text-lg font-semibold tracking-tight">Revision timeline</h2>
          {revisions.length > 0 ? (
            <Link
              href={`/knowledge/beliefs/${encodeURIComponent(id)}/timeline`}
              className="text-xs font-mono uppercase tracking-wider text-primary hover:underline"
            >
              full timeline →
            </Link>
          ) : null}
        </div>
        <RevisionTimeline revisions={revisions} />
      </section>
    </main>
  );
}

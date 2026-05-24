import Link from 'next/link';
import { notFound } from 'next/navigation';

import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { api, ApiError } from '@/lib/api';
import { formatConfidence, formatDateTime } from '@/lib/format';

export const dynamic = 'force-dynamic';

export default async function ClaimDetailPage(props: { params: Promise<{ id: string }> }) {
  const { id } = await props.params;
  let detail;
  try {
    detail = await api.claim(id);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) notFound();
    throw e;
  }
  const { claim, source, subject_entity } = detail;

  return (
    <main className="space-y-6">
      <header className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline" className="font-mono">{claim.predicate}</Badge>
          <Badge variant="secondary">{formatConfidence(claim.confidence)}</Badge>
          <Badge variant={claim.status === 'active' ? 'secondary' : 'outline'}>{claim.status}</Badge>
        </div>
        <h1 className="text-xl font-semibold leading-snug">
          {subject_entity ? (
            <Link href={`/entities/${subject_entity.id}`} className="hover:underline">
              {subject_entity.canonical_name}
            </Link>
          ) : (
            <span className="text-muted-foreground">unknown subject</span>
          )}{' '}
          <span className="text-muted-foreground">— {claim.predicate}</span>
        </h1>
        <p className="text-xs text-muted-foreground">
          Extracted by <span className="font-mono">{claim.extracted_by_agent}</span> on{' '}
          {formatDateTime(claim.extracted_at)}
        </p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle>Raw excerpt</CardTitle>
        </CardHeader>
        <CardContent>
          <pre className="whitespace-pre-wrap break-words font-sans text-sm">{claim.raw_excerpt}</pre>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Object payload</CardTitle>
        </CardHeader>
        <CardContent>
          <pre className="overflow-auto whitespace-pre-wrap break-words font-mono text-xs">
            {JSON.stringify(claim.object, null, 2)}
          </pre>
        </CardContent>
      </Card>

      {source ? (
        <Card>
          <CardHeader>
            <CardTitle>Source</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            <div>
              <Link href={`/sources/${source.id}`} className="font-medium hover:underline">
                {source.url}
              </Link>
            </div>
            <div className="text-xs text-muted-foreground">
              {source.type} · reliability {formatConfidence(source.reliability_prior)} · published{' '}
              {formatDateTime(source.published_at)}
            </div>
          </CardContent>
        </Card>
      ) : null}
    </main>
  );
}

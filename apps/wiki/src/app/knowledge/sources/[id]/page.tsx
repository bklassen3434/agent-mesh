import Link from 'next/link';
import { notFound } from 'next/navigation';

import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { api, ApiError } from '@/lib/api';
import { formatConfidence, formatDateTime } from '@/lib/format';

export const dynamic = 'force-dynamic';

export default async function SourceDetailPage(props: { params: Promise<{ id: string }> }) {
  const { id } = await props.params;
  let detail;
  try {
    detail = await api.source(id);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) notFound();
    throw e;
  }
  const { source, claims } = detail;

  return (
    <main className="space-y-6">
      <header className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="secondary">{source.type}</Badge>
          <span className="text-xs text-muted-foreground">
            reliability {formatConfidence(source.reliability_prior)}
          </span>
        </div>
        <h1 className="break-all text-lg font-semibold leading-snug">
          <a href={source.url} target="_blank" rel="noreferrer" className="hover:underline">
            {source.url}
          </a>
        </h1>
        <p className="text-xs text-muted-foreground">
          {source.author ? <>by {source.author} · </> : null}
          published {formatDateTime(source.published_at)} · fetched{' '}
          {formatDateTime(source.fetched_at)}
        </p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle>Claims from this source ({claims.length})</CardTitle>
        </CardHeader>
        <CardContent>
          {claims.length === 0 ? (
            <div className="text-sm text-muted-foreground">No claims extracted yet.</div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Predicate</TableHead>
                  <TableHead>Excerpt</TableHead>
                  <TableHead>Confidence</TableHead>
                  <TableHead></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {claims.map((c) => (
                  <TableRow key={c.id}>
                    <TableCell className="font-mono text-xs">{c.predicate}</TableCell>
                    <TableCell className="max-w-md text-xs text-muted-foreground">{c.raw_excerpt}</TableCell>
                    <TableCell>{formatConfidence(c.confidence)}</TableCell>
                    <TableCell>
                      <Link href={`/knowledge/claims/${c.id}`} className="text-xs hover:underline">view →</Link>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </main>
  );
}

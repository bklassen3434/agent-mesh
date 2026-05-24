import Link from 'next/link';
import { notFound } from 'next/navigation';

import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { api, ApiError } from '@/lib/api';
import { formatConfidence, formatDateTime } from '@/lib/format';

export const dynamic = 'force-dynamic';

export default async function EntityDetailPage(props: { params: Promise<{ id: string }> }) {
  const { id } = await props.params;
  let detail;
  try {
    detail = await api.entity(id);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) notFound();
    throw e;
  }

  const { entity, claims, relationships } = detail;

  return (
    <main className="space-y-6">
      <div>
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">{entity.canonical_name}</h1>
          <Badge variant="secondary">{entity.type}</Badge>
        </div>
        {entity.aliases?.length ? (
          <p className="mt-1 text-sm text-muted-foreground">
            aka {entity.aliases.join(', ')}
          </p>
        ) : null}
        <p className="mt-1 text-xs text-muted-foreground">
          First seen {formatDateTime(entity.created_at)} · Last seen{' '}
          {formatDateTime(entity.last_seen_at)}
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Claims about this entity ({claims.length})</CardTitle>
        </CardHeader>
        <CardContent>
          {claims.length === 0 ? (
            <div className="text-sm text-muted-foreground">No claims recorded.</div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Predicate</TableHead>
                  <TableHead>Excerpt</TableHead>
                  <TableHead>Confidence</TableHead>
                  <TableHead>Status</TableHead>
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
                      <Badge variant={c.status === 'active' ? 'secondary' : 'outline'}>{c.status}</Badge>
                    </TableCell>
                    <TableCell>
                      <Link href={`/claims/${c.id}`} className="text-xs hover:underline">
                        view →
                      </Link>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {relationships.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>Relationships ({relationships.length})</CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Type</TableHead>
                  <TableHead>From</TableHead>
                  <TableHead>To</TableHead>
                  <TableHead>Confidence</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {relationships.map((r) => (
                  <TableRow key={r.id}>
                    <TableCell className="font-mono text-xs">{r.type}</TableCell>
                    <TableCell className="font-mono text-xs">
                      <Link href={`/entities/${r.from_entity_id}`} className="hover:underline">
                        {r.from_entity_id === entity.id ? '(this)' : r.from_entity_id.slice(0, 8)}
                      </Link>
                    </TableCell>
                    <TableCell className="font-mono text-xs">
                      <Link href={`/entities/${r.to_entity_id}`} className="hover:underline">
                        {r.to_entity_id === entity.id ? '(this)' : r.to_entity_id.slice(0, 8)}
                      </Link>
                    </TableCell>
                    <TableCell>{formatConfidence(r.confidence)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      ) : null}
    </main>
  );
}

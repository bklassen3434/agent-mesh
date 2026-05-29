import Link from 'next/link';

import { EmptyState } from '@/components/empty-state';
import { Pagination } from '@/components/pagination';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { api } from '@/lib/api';
import { formatConfidence, formatDateTime } from '@/lib/format';

export const dynamic = 'force-dynamic';

type SP = { [k: string]: string | string[] | undefined };

function pick(sp: SP, key: string): string | undefined {
  const v = sp[key];
  return Array.isArray(v) ? v[0] : v;
}

export default async function ClaimsPage(props: { searchParams: Promise<SP> }) {
  const sp = await props.searchParams;
  const predicate = pick(sp, 'predicate');
  const source_id = pick(sp, 'source_id');
  const limit = Number(pick(sp, 'limit') ?? 50);
  const offset = Number(pick(sp, 'offset') ?? 0);

  const page = await api.listClaims({ predicate, source_id, limit, offset });

  return (
    <main className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Claims</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Immutable observations extracted from sources. Click to see the source and subject entity.
        </p>
      </div>

      <form className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <label htmlFor="predicate" className="text-xs text-muted-foreground">Predicate</label>
          <input
            id="predicate"
            name="predicate"
            defaultValue={predicate ?? ''}
            className="h-9 rounded-md border border-input bg-background px-3 text-sm"
            placeholder="e.g. achieves_score"
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
        <EmptyState title="No claims match" />
      ) : (
        <>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Predicate</TableHead>
                <TableHead>Excerpt</TableHead>
                <TableHead>Confidence</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Extracted</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {page.items.map((c) => (
                <TableRow key={c.id}>
                  <TableCell className="font-mono text-xs">
                    <Link href={`/knowledge/claims/${c.id}`} className="hover:underline">{c.predicate}</Link>
                  </TableCell>
                  <TableCell className="max-w-md text-xs text-muted-foreground">{c.raw_excerpt}</TableCell>
                  <TableCell>{formatConfidence(c.confidence)}</TableCell>
                  <TableCell>
                    <Badge variant={c.status === 'active' ? 'secondary' : 'outline'}>{c.status}</Badge>
                  </TableCell>
                  <TableCell className="text-xs">{formatDateTime(c.extracted_at)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>

          <Pagination
            total={page.total}
            limit={page.limit}
            offset={page.offset}
            basePath="/knowledge/claims"
            searchParams={{ predicate, source_id }}
          />
        </>
      )}
    </main>
  );
}

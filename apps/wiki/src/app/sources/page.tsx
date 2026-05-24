import Link from 'next/link';

import { EmptyState } from '@/components/empty-state';
import { Pagination } from '@/components/pagination';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { api } from '@/lib/api';
import { formatConfidence, formatDateTime, formatNumber } from '@/lib/format';

export const dynamic = 'force-dynamic';

type SP = { [k: string]: string | string[] | undefined };

function pick(sp: SP, key: string): string | undefined {
  const v = sp[key];
  return Array.isArray(v) ? v[0] : v;
}

export default async function SourcesPage(props: { searchParams: Promise<SP> }) {
  const sp = await props.searchParams;
  const limit = Number(pick(sp, 'limit') ?? 50);
  const offset = Number(pick(sp, 'offset') ?? 0);

  const page = await api.listSources({ limit, offset });

  return (
    <main className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Sources</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Documents the mesh has read. Each row shows how many claims were extracted from it.
        </p>
      </div>

      {page.items.length === 0 ? (
        <EmptyState title="No sources yet" description="Run the pipeline to ingest sources." />
      ) : (
        <>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>URL</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Reliability</TableHead>
                <TableHead>Claims</TableHead>
                <TableHead>Fetched</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {page.items.map((row) => (
                <TableRow key={row.source.id}>
                  <TableCell className="max-w-md truncate">
                    <Link href={`/sources/${row.source.id}`} className="hover:underline">
                      {row.source.url}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <Badge variant="secondary">{row.source.type}</Badge>
                  </TableCell>
                  <TableCell>{formatConfidence(row.source.reliability_prior)}</TableCell>
                  <TableCell>{formatNumber(row.claim_count)}</TableCell>
                  <TableCell className="text-xs">{formatDateTime(row.source.fetched_at)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>

          <Pagination
            total={page.total}
            limit={page.limit}
            offset={page.offset}
            basePath="/sources"
          />
        </>
      )}
    </main>
  );
}

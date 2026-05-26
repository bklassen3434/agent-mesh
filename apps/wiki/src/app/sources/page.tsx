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

const SOURCE_TYPES = [
  'arxiv',
  'hn_post',
  'github',
  'bluesky',
  'reddit',
  'blog',
  'leaderboard',
  'agent_reasoning',
] as const;

export default async function SourcesPage(props: { searchParams: Promise<SP> }) {
  const sp = await props.searchParams;
  const limit = Number(pick(sp, 'limit') ?? 50);
  const offset = Number(pick(sp, 'offset') ?? 0);
  const activeType = pick(sp, 'type');

  const page = await api.listSources({ type: activeType, limit, offset });

  return (
    <main className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Sources</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Documents the mesh has read. Each row shows how many claims were extracted from it.
        </p>
      </div>

      <SourceTypeFilter active={activeType} />

      {page.items.length === 0 ? (
        <EmptyState
          title="No sources yet"
          description={
            activeType
              ? `No sources of type "${activeType}" yet. Try a different filter or run the pipeline.`
              : 'Run the pipeline to ingest sources.'
          }
        />
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
            searchParams={activeType ? { type: activeType } : undefined}
          />
        </>
      )}
    </main>
  );
}

function SourceTypeFilter({ active }: { active: string | undefined }) {
  const baseChip =
    'inline-flex items-center rounded-full border px-3 py-1 text-xs transition-colors';
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-xs text-muted-foreground mr-1">Filter:</span>
      <Link
        href="/sources"
        className={`${baseChip} ${
          !active
            ? 'border-foreground bg-foreground text-background'
            : 'border-border hover:bg-muted'
        }`}
      >
        All
      </Link>
      {SOURCE_TYPES.map((t) => (
        <Link
          key={t}
          href={`/sources?type=${t}`}
          className={`${baseChip} ${
            active === t
              ? 'border-foreground bg-foreground text-background'
              : 'border-border hover:bg-muted'
          }`}
        >
          {t}
        </Link>
      ))}
    </div>
  );
}

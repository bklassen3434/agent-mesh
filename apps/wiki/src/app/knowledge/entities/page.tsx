import Link from 'next/link';

import { EmptyState } from '@/components/empty-state';
import { EntityFilter } from '@/components/entity-filter';
import { Pagination } from '@/components/pagination';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { api } from '@/lib/api';
import { formatDateTime } from '@/lib/format';

export const dynamic = 'force-dynamic';

type SP = { [k: string]: string | string[] | undefined };

function pick(sp: SP, key: string): string | undefined {
  const v = sp[key];
  return Array.isArray(v) ? v[0] : v;
}

export default async function EntitiesPage(props: { searchParams: Promise<SP> }) {
  const sp = await props.searchParams;
  const q = pick(sp, 'q');
  const type = pick(sp, 'type');
  const limit = Number(pick(sp, 'limit') ?? 50);
  const offset = Number(pick(sp, 'offset') ?? 0);

  const page = await api.listEntities({ q, type, limit, offset });

  return (
    <main className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Entities</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          People, models, benchmarks, papers — the canonical things the mesh tracks.
        </p>
      </div>

      <EntityFilter initialQ={q} initialType={type} />

      {page.items.length === 0 ? (
        <EmptyState
          title="No entities match"
          description={q || type ? 'Try clearing filters.' : 'Run the pipeline to populate the mesh.'}
        />
      ) : (
        <>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Aliases</TableHead>
                <TableHead>First seen</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {page.items.map((e) => (
                <TableRow key={e.id}>
                  <TableCell>
                    <Link href={`/knowledge/entities/${e.id}`} className="font-medium hover:underline">
                      {e.canonical_name}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <Badge variant="secondary">{e.type}</Badge>
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {e.aliases?.length ? e.aliases.join(', ') : '—'}
                  </TableCell>
                  <TableCell className="text-xs">{formatDateTime(e.created_at)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>

          <Pagination
            total={page.total}
            limit={page.limit}
            offset={page.offset}
            basePath="/knowledge/entities"
            searchParams={{ q, type }}
          />
        </>
      )}
    </main>
  );
}

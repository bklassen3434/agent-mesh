import Link from 'next/link';

import { EmptyState } from '@/components/empty-state';
import { StatTile } from '@/components/stat-tile';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { api } from '@/lib/api';
import { formatDateTime, formatNumber } from '@/lib/format';

export const dynamic = 'force-dynamic';

export default async function HomePage() {
  const [stats, runs, beliefs] = await Promise.all([
    api.stats(),
    api.pipelineRuns(5),
    api.listBeliefs({ limit: 5 }),
  ]);

  if (stats.entities === 0 && stats.claims === 0 && stats.beliefs === 0) {
    return (
      <main className="space-y-6">
        <h1 className="text-3xl font-semibold tracking-tight">Agent Mesh</h1>
        <p className="text-muted-foreground">
          A persistent multi-agent system tracking AI/robotics research.
        </p>
        <EmptyState
          title="No data yet"
          description={
            <>
              The mesh hasn&apos;t ingested anything. Run{' '}
              <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">make pipeline</code>{' '}
              to populate it.
            </>
          }
        />
      </main>
    );
  }

  return (
    <main className="space-y-8">
      <div>
        <h1 className="text-3xl font-semibold tracking-tight">Agent Mesh</h1>
        <p className="mt-1 text-muted-foreground">
          {formatNumber(stats.entities)} entities · {formatNumber(stats.claims)} claims ·{' '}
          {formatNumber(stats.beliefs)} beliefs synthesised from{' '}
          {formatNumber(stats.sources)} sources.
        </p>
      </div>

      <section className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <StatTile label="Entities" value={stats.entities} />
        <StatTile label="Claims" value={stats.claims} />
        <StatTile label="Beliefs" value={stats.beliefs} />
        <StatTile label="Sources" value={stats.sources} />
      </section>

      <section className="grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Recent pipeline runs</CardTitle>
          </CardHeader>
          <CardContent>
            {runs.length === 0 ? (
              <div className="text-sm text-muted-foreground">No runs recorded.</div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Started</TableHead>
                    <TableHead>Papers</TableHead>
                    <TableHead>Claims</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {runs.map((r) => (
                    <TableRow key={r.id}>
                      <TableCell className="font-mono text-xs">{formatDateTime(r.started_at)}</TableCell>
                      <TableCell>{formatNumber(r.papers_scouted)}</TableCell>
                      <TableCell>{formatNumber(r.claims_inserted)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Most recently revised beliefs</CardTitle>
          </CardHeader>
          <CardContent>
            {beliefs.items.length === 0 ? (
              <div className="text-sm text-muted-foreground">No beliefs yet.</div>
            ) : (
              <ul className="space-y-3">
                {beliefs.items.map((b) => (
                  <li key={b.id}>
                    <Link
                      href={`/knowledge/beliefs/${b.id}`}
                      className="block hover:underline"
                    >
                      <div className="text-sm font-medium">{b.topic}</div>
                      <div className="mt-0.5 text-xs text-muted-foreground">
                        {b.statement}
                      </div>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </section>
    </main>
  );
}

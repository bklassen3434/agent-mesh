import Link from 'next/link';

import { Badge } from '@/components/ui/badge';
import type { ClaimWithContext } from '@/lib/api';
import { formatConfidence } from '@/lib/format';

export function ClaimCard({ entry }: { entry: ClaimWithContext }) {
  const { claim, source, subject_entity } = entry;
  return (
    <div className="rounded-md border border-border p-4">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Badge variant="outline" className="font-mono">{claim.predicate}</Badge>
        <Badge variant="secondary">{formatConfidence(claim.confidence)}</Badge>
        <Badge variant={claim.status === 'active' ? 'secondary' : 'outline'}>{claim.status}</Badge>
      </div>
      <pre className="mt-3 whitespace-pre-wrap break-words font-sans text-sm">{claim.raw_excerpt}</pre>
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {subject_entity ? (
          <span>
            About{' '}
            <Link href={`/knowledge/entities/${subject_entity.id}`} className="underline-offset-2 hover:underline">
              {subject_entity.canonical_name}
            </Link>
          </span>
        ) : null}
        {source ? (
          <span>
            From{' '}
            <Link href={`/knowledge/sources/${source.id}`} className="underline-offset-2 hover:underline">
              {source.url}
            </Link>
          </span>
        ) : null}
        <Link href={`/knowledge/claims/${claim.id}`} className="ml-auto hover:underline">
          claim detail →
        </Link>
      </div>
    </div>
  );
}

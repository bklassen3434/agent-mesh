import Link from 'next/link';

import { cn } from '@/lib/utils';

export function Pagination({
  total,
  limit,
  offset,
  basePath,
  searchParams,
}: {
  total: number;
  limit: number;
  offset: number;
  basePath: string;
  searchParams?: Record<string, string | undefined>;
}) {
  if (total <= limit) return null;
  const page = Math.floor(offset / limit) + 1;
  const totalPages = Math.ceil(total / limit);
  const prevOffset = Math.max(offset - limit, 0);
  const nextOffset = offset + limit;

  function urlFor(o: number): string {
    const sp = new URLSearchParams();
    for (const [k, v] of Object.entries(searchParams ?? {})) {
      if (v !== undefined && k !== 'offset') sp.set(k, v);
    }
    sp.set('limit', String(limit));
    sp.set('offset', String(o));
    return `${basePath}?${sp.toString()}`;
  }

  const linkClass =
    'rounded-md border border-input px-3 py-1.5 text-sm font-medium transition-colors hover:bg-accent';
  const disabledClass = 'pointer-events-none opacity-50';

  return (
    <nav className="mt-4 flex items-center justify-between text-sm">
      <div className="text-muted-foreground">
        Page {page} of {totalPages} · {total.toLocaleString()} total
      </div>
      <div className="flex gap-2">
        <Link
          href={urlFor(prevOffset)}
          className={cn(linkClass, offset === 0 ? disabledClass : '')}
          aria-disabled={offset === 0}
        >
          Previous
        </Link>
        <Link
          href={urlFor(nextOffset)}
          className={cn(linkClass, nextOffset >= total ? disabledClass : '')}
          aria-disabled={nextOffset >= total}
        >
          Next
        </Link>
      </div>
    </nav>
  );
}

'use client';

import Link from 'next/link';

export default function BriefingError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <div className="rounded-md border border-destructive/40 bg-destructive/5 p-6 text-sm">
      <h2 className="text-base font-semibold text-destructive">Briefing failed to load</h2>
      <p className="mt-2 text-muted-foreground">{error.message}</p>
      <div className="mt-4 flex gap-3 text-xs">
        <button onClick={reset} className="underline">
          Retry
        </button>
        <Link href="/" className="underline">
          Back to home
        </Link>
      </div>
    </div>
  );
}

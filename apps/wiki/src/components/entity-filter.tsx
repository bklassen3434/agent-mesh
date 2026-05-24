'use client';

import { useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useState } from 'react';

const ENTITY_TYPES = ['model', 'paper', 'benchmark', 'method', 'person', 'lab', 'repo', 'concept'];

export function EntityFilter({
  initialQ,
  initialType,
}: {
  initialQ?: string;
  initialType?: string;
}) {
  const router = useRouter();
  const sp = useSearchParams();
  const [q, setQ] = useState(initialQ ?? '');
  const [type, setType] = useState(initialType ?? '');

  const apply = useCallback(
    (next: { q?: string; type?: string }) => {
      const params = new URLSearchParams(sp?.toString() ?? '');
      params.set('offset', '0');
      const newQ = next.q ?? q;
      const newType = next.type ?? type;
      if (newQ) params.set('q', newQ);
      else params.delete('q');
      if (newType) params.set('type', newType);
      else params.delete('type');
      router.push(`/entities?${params.toString()}`);
    },
    [router, sp, q, type],
  );

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        apply({});
      }}
      className="flex flex-wrap items-end gap-3"
    >
      <div className="flex flex-col gap-1">
        <label htmlFor="entity-q" className="text-xs text-muted-foreground">
          Name contains
        </label>
        <input
          id="entity-q"
          className="h-9 rounded-md border border-input bg-background px-3 text-sm"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="e.g. GPT"
        />
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor="entity-type" className="text-xs text-muted-foreground">
          Type
        </label>
        <select
          id="entity-type"
          className="h-9 rounded-md border border-input bg-background px-3 text-sm"
          value={type}
          onChange={(e) => {
            setType(e.target.value);
            apply({ type: e.target.value });
          }}
        >
          <option value="">All types</option>
          {ENTITY_TYPES.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </div>
      <button
        type="submit"
        className="h-9 rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground hover:bg-primary/90"
      >
        Filter
      </button>
    </form>
  );
}

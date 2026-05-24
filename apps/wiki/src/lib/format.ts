export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—';
  try {
    const d = new Date(value);
    if (isNaN(d.getTime())) return value;
    return d.toLocaleString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return value;
  }
}

export function formatNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—';
  return new Intl.NumberFormat().format(n);
}

export function formatConfidence(c: number | null | undefined): string {
  if (c === null || c === undefined) return '—';
  return `${Math.round(c * 100)}%`;
}

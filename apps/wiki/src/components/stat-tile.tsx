import { Card, CardContent } from '@/components/ui/card';
import { formatNumber } from '@/lib/format';

export function StatTile({ label, value }: { label: string; value: number }) {
  return (
    <Card>
      <CardContent className="pt-6">
        <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
        <div className="mt-2 text-2xl font-semibold tracking-tight">{formatNumber(value)}</div>
      </CardContent>
    </Card>
  );
}

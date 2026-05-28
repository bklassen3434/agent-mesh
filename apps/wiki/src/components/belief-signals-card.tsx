import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import type { BeliefSignals } from '@/lib/api';

const SCORE_LABELS = [
  { upper: 0.35, label: 'hype-shaped', tone: 'text-amber-700 dark:text-amber-400' },
  { upper: 0.65, label: 'mixed signal', tone: 'text-muted-foreground' },
  { upper: 1.01, label: 'substantive', tone: 'text-emerald-700 dark:text-emerald-400' },
];

function scoreBucket(score: number): { label: string; tone: string } {
  for (const b of SCORE_LABELS) {
    if (score < b.upper) return { label: b.label, tone: b.tone };
  }
  return { label: 'substantive', tone: 'text-emerald-700 dark:text-emerald-400' };
}

export function BeliefSignalsCard({ signals }: { signals: BeliefSignals }) {
  const bucket = scoreBucket(signals.hype_substance_score);
  const percent = Math.round(signals.hype_substance_score * 100);
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Derived signals</CardTitle>
        <p className="text-xs text-muted-foreground">
          Recomputed on read from the belief_hype_substance + belief_reproduction
          DuckDB views. Informational — does not drive any mesh behavior.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-baseline justify-between">
          <div>
            <div className="text-xs uppercase tracking-wide text-muted-foreground">
              hype ↔ substance
            </div>
            <div className="flex items-baseline gap-2">
              <span className={`text-2xl font-semibold tabular-nums ${bucket.tone}`}>
                {percent}
              </span>
              <span className="text-sm text-muted-foreground">/ 100</span>
              <span className={`text-xs uppercase tracking-wide ${bucket.tone}`}>
                {bucket.label}
              </span>
            </div>
          </div>
          <div className="text-right text-xs text-muted-foreground">
            anchored at 50 — no evidence and no attacks
          </div>
        </div>
        <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
          <SignalRow
            label="Reproduction"
            value={signals.reproduction_count}
            hint="distinct source types backing one canonical claim"
          />
          <SignalRow
            label="Source diversity"
            value={signals.source_type_diversity}
            hint="distinct source types in supporting claims"
          />
          <SignalRow
            label="Skeptic attacks"
            value={signals.skeptic_counter_claim_count}
            hint="counter-claims attached to this belief"
            tone={signals.skeptic_counter_claim_count > 0 ? 'warn' : undefined}
          />
          <SignalRow
            label="Severe failures"
            value={signals.severe_failure_mode_count}
            hint="methodological_flaw, cherry_picked, contradicted_by_source"
            tone={signals.severe_failure_mode_count > 0 ? 'warn' : undefined}
          />
          <SignalRow
            label="Claims in last 30d"
            value={signals.claims_last_30d}
            hint="recent supporting-claim velocity"
          />
        </div>
      </CardContent>
    </Card>
  );
}

function SignalRow({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: number;
  hint: string;
  tone?: 'warn';
}) {
  return (
    <div className="space-y-0.5">
      <div className="flex items-baseline gap-2">
        <Badge
          variant={tone === 'warn' ? 'destructive' : 'outline'}
          className="tabular-nums font-mono"
        >
          {value}
        </Badge>
        <span className="text-xs font-medium">{label}</span>
      </div>
      <div className="text-xs text-muted-foreground pl-1">{hint}</div>
    </div>
  );
}

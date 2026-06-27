'use client';

import { CitationPill, type CitationKind } from '@/components/evidence-dialog';
import { Markdown } from '@/components/markdown';
import { Badge } from '@/components/ui/badge';
import type { Answer, Coverage } from '@/lib/api';

const COVERAGE_META: Record<
  Coverage,
  { label: string; variant: 'default' | 'secondary' | 'destructive' }
> = {
  well_supported: { label: 'Well supported', variant: 'default' },
  thin: { label: 'Thin evidence', variant: 'secondary' },
  uncovered: { label: 'Not covered', variant: 'destructive' },
};

/**
 * The grounded answer body — markdown prose, citation pills, and caveats.
 * Citations open the evidence popup in place (see EvidenceProvider), so the
 * reader never leaves the chat. Wrap a list of answers in a single
 * <EvidenceProvider> so they share one popup.
 */
export function AnswerView({ answer }: { answer: Answer }) {
  const cov = COVERAGE_META[answer.coverage];
  const citations = answer.citations ?? [];
  const caveats = answer.caveats ?? [];
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Answer
        </span>
        <Badge variant={cov.variant} aria-label={`coverage: ${answer.coverage}`}>
          {cov.label}
        </Badge>
      </div>

      <Markdown>{answer.answer_markdown}</Markdown>

      {citations.length > 0 && (
        <div className="space-y-2">
          <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Citations
          </div>
          <div className="flex flex-wrap gap-2">
            {citations.map((c, i) => (
              <CitationPill
                key={`${c.kind}:${c.id}:${i}`}
                kind={c.kind as CitationKind}
                id={c.id}
                quote={c.quote}
              />
            ))}
          </div>
        </div>
      )}

      {caveats.length > 0 && (
        <div className="space-y-1">
          <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Caveats
          </div>
          <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
            {caveats.map((cav, i) => (
              <li key={i}>{cav}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

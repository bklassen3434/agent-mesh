'use client';

import Link from 'next/link';
import { type FormEvent, type ReactNode, useState } from 'react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { type Answer, ApiError, type Coverage, api } from '@/lib/api';

const KIND_SEGMENT: Record<string, string> = {
  belief: 'beliefs',
  claim: 'claims',
  entity: 'entities',
};

function citationHref(kind: string, id: string): string {
  const seg = KIND_SEGMENT[kind] ?? 'beliefs';
  return `/knowledge/${seg}/${encodeURIComponent(id)}`;
}

const COVERAGE_META: Record<
  Coverage,
  { label: string; variant: 'default' | 'secondary' | 'destructive' }
> = {
  well_supported: { label: 'Well supported', variant: 'default' },
  thin: { label: 'Thin evidence', variant: 'secondary' },
  uncovered: { label: 'Not covered', variant: 'destructive' },
};

// Render answer markdown, turning inline [kind:id] citation tokens into links to
// the existing detail pages. Plain text is shown verbatim (whitespace-pre-wrap
// preserves the markdown's line breaks); a heavier markdown renderer is a
// deliberate non-goal for v1.
const CITATION_RE = /\[(belief|claim|entity):([^\]]+)\]/g;

function renderWithCitations(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  let key = 0;
  CITATION_RE.lastIndex = 0;
  while ((m = CITATION_RE.exec(text)) !== null) {
    if (m.index > last) out.push(<span key={key++}>{text.slice(last, m.index)}</span>);
    const [, kind, id] = m;
    out.push(
      <Link
        key={key++}
        href={citationHref(kind, id)}
        className="mx-0.5 rounded bg-muted px-1 text-xs font-medium text-foreground hover:underline"
      >
        [{kind}]
      </Link>,
    );
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(<span key={key++}>{text.slice(last)}</span>);
  return out;
}

export function AskPanel({ initialField }: { initialField: string }) {
  const [question, setQuestion] = useState('');
  const [field, setField] = useState(initialField);
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q || loading) return;
    setLoading(true);
    setError(null);
    setAnswer(null);
    try {
      const res = await api.ask(q, field.trim() || 'ai-robotics');
      setAnswer(res);
    } catch (err) {
      const code = err instanceof ApiError ? err.status : 0;
      setError(
        code === 422
          ? 'Please enter a question.'
          : 'Could not get an answer right now. Please try again.',
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="space-y-6">
      <form onSubmit={submit} className="space-y-3">
        <textarea
          aria-label="Question"
          name="question"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask about the state of the field, a model vs another, how strong the evidence is…"
          rows={3}
          className="w-full resize-y rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring"
        />
        <div className="flex items-center gap-3">
          <label className="text-xs text-muted-foreground" htmlFor="ask-field">
            Field
          </label>
          <input
            id="ask-field"
            aria-label="Field"
            value={field}
            onChange={(e) => setField(e.target.value)}
            className="w-44 rounded-md border border-border bg-background px-2 py-1 text-sm outline-none focus:ring-2 focus:ring-ring"
          />
          <Button type="submit" disabled={loading || !question.trim()} className="ml-auto">
            {loading ? 'Asking…' : 'Ask'}
          </Button>
        </div>
      </form>

      {loading && (
        <div
          role="status"
          className="h-24 animate-pulse rounded-lg border border-border bg-muted/40"
        />
      )}

      {error && (
        <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      )}

      {answer && !loading && <AnswerCard answer={answer} />}
    </div>
  );
}

function AnswerCard({ answer }: { answer: Answer }) {
  const cov = COVERAGE_META[answer.coverage];
  const citations = answer.citations ?? [];
  const caveats = answer.caveats ?? [];
  return (
    <Card data-testid="ask-answer">
      <CardHeader className="flex flex-row items-center justify-between gap-3 space-y-0">
        <CardTitle className="text-base">Answer</CardTitle>
        <Badge variant={cov.variant} aria-label={`coverage: ${answer.coverage}`}>
          {cov.label}
        </Badge>
      </CardHeader>
      <CardContent className="space-y-5">
        <div className="whitespace-pre-wrap text-sm leading-relaxed">
          {renderWithCitations(answer.answer_markdown)}
        </div>

        {citations.length > 0 && (
          <div className="space-y-2">
            <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Citations
            </div>
            <div className="flex flex-wrap gap-2">
              {citations.map((c, i) => (
                <Link
                  key={`${c.kind}:${c.id}:${i}`}
                  href={citationHref(c.kind, c.id)}
                  title={c.quote}
                  className="inline-flex items-center gap-1 rounded-full border border-border px-2.5 py-0.5 text-xs hover:bg-accent"
                >
                  <span className="font-medium">{c.kind}</span>
                  <span className="text-muted-foreground">
                    {c.id.length > 10 ? `${c.id.slice(0, 8)}…` : c.id}
                  </span>
                </Link>
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
      </CardContent>
    </Card>
  );
}

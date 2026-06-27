'use client';

import Link from 'next/link';
import { type FormEvent, type ReactNode, useEffect, useRef, useState } from 'react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { type Answer, ApiError, type Coverage, type QuotaStatus, api } from '@/lib/api';
import type { Role } from '@/lib/auth';

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

type Turn =
  | { role: 'user'; text: string }
  | { role: 'assistant'; answer: Answer };

export function ChatPanel({ field, role }: { field: string; role: Role }) {
  const isBeta = role === 'beta';
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [locked, setLocked] = useState(false);
  const [quota, setQuota] = useState<QuotaStatus | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);

  // Beta visitors see how many questions remain today; admins are unlimited.
  useEffect(() => {
    if (!isBeta) return;
    api
      .askQuota()
      .then((q) => {
        setQuota(q);
        if (q.remaining <= 0) setLocked(true);
      })
      .catch(() => {});
  }, [isBeta]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [turns, loading]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q || loading || locked) return;
    setLoading(true);
    setError(null);
    setTurns((t) => [...t, { role: 'user', text: q }]);
    setQuestion('');
    try {
      const res = await api.ask(q, field);
      setTurns((t) => [...t, { role: 'assistant', answer: res }]);
      if (isBeta) api.askQuota().then((quo) => {
        setQuota(quo);
        if (quo.remaining <= 0) setLocked(true);
      }).catch(() => {});
    } catch (err) {
      const code = err instanceof ApiError ? err.status : 0;
      if (code === 429) {
        setLocked(true);
        if (isBeta) api.askQuota().then(setQuota).catch(() => {});
      } else {
        setError(
          code === 422
            ? 'Please enter a question.'
            : 'Could not get an answer right now. Please try again.',
        );
      }
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="mx-auto flex min-h-[70vh] max-w-3xl flex-col">
      <header className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight">Ask the mesh</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Grounded answers from the <span className="font-medium text-foreground">{field}</span>{' '}
          knowledge base — every claim links back to its evidence.
        </p>
      </header>

      <div className="flex-1 space-y-6">
        {turns.length === 0 && !loading && (
          <div className="rounded-lg border border-dashed border-border px-6 py-10 text-center text-sm text-muted-foreground">
            Ask about the state of the field, how two systems compare, or how strong the
            evidence is for a claim.
          </div>
        )}

        {turns.map((t, i) =>
          t.role === 'user' ? (
            <div key={i} className="flex justify-end">
              <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-primary px-4 py-2 text-sm text-primary-foreground">
                {t.text}
              </div>
            </div>
          ) : (
            <AssistantTurn key={i} answer={t.answer} />
          ),
        )}

        {loading && (
          <div
            role="status"
            className="h-20 max-w-[85%] animate-pulse rounded-2xl border border-border bg-muted/40"
          />
        )}

        {error && (
          <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
            {error}
          </div>
        )}
        <div ref={endRef} />
      </div>

      <div className="sticky bottom-0 mt-6 bg-background pb-4 pt-2">
        {locked ? (
          <LockedNotice quota={quota} />
        ) : (
          <form onSubmit={submit} className="space-y-2">
            <textarea
              aria-label="Question"
              name="question"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  submit(e as unknown as FormEvent);
                }
              }}
              placeholder="Ask a question…"
              rows={2}
              className="w-full resize-none rounded-xl border border-border bg-background px-4 py-3 text-sm outline-none focus:ring-2 focus:ring-ring"
            />
            <div className="flex items-center justify-between">
              <span className="text-xs text-muted-foreground">
                {isBeta && quota
                  ? `${quota.remaining} of ${quota.limit} questions left today`
                  : 'Enter to send · Shift+Enter for a new line'}
              </span>
              <Button type="submit" disabled={loading || !question.trim()}>
                {loading ? 'Asking…' : 'Ask'}
              </Button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

function LockedNotice({ quota }: { quota: QuotaStatus | null }) {
  return (
    <div className="rounded-xl border border-border bg-muted/40 px-4 py-4 text-sm">
      <p className="font-medium">You&apos;ve used today&apos;s {quota?.limit ?? ''} questions.</p>
      <p className="mt-1 text-muted-foreground">
        The limit resets tomorrow.{' '}
        <Link href="/login" className="font-medium text-foreground hover:underline">
          Sign in as admin
        </Link>{' '}
        for unlimited questions.
      </p>
    </div>
  );
}

function AssistantTurn({ answer }: { answer: Answer }) {
  const cov = COVERAGE_META[answer.coverage];
  const citations = answer.citations ?? [];
  const caveats = answer.caveats ?? [];
  return (
    <div data-testid="ask-answer" className="space-y-4 rounded-2xl rounded-bl-sm border border-border bg-card px-4 py-4">
      <div className="flex items-center justify-between gap-3">
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Answer
        </span>
        <Badge variant={cov.variant} aria-label={`coverage: ${answer.coverage}`}>
          {cov.label}
        </Badge>
      </div>
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
    </div>
  );
}

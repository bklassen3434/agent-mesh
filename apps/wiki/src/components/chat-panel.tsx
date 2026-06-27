'use client';

import { type FormEvent, useEffect, useRef, useState } from 'react';

import { AnswerView } from '@/components/answer-view';
import { EvidenceProvider } from '@/components/evidence-dialog';
import { Button } from '@/components/ui/button';
import { type Answer, ApiError, type QuotaStatus, api } from '@/lib/api';
import type { Role } from '@/lib/auth';

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
    <EvidenceProvider field={field} role={role}>
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
    </EvidenceProvider>
  );
}

function LockedNotice({ quota }: { quota: QuotaStatus | null }) {
  return (
    <div className="rounded-xl border border-border bg-muted/40 px-4 py-4 text-sm">
      <p className="font-medium">You&apos;ve used today&apos;s {quota?.limit ?? ''} questions.</p>
      <p className="mt-1 text-muted-foreground">The limit resets tomorrow.</p>
    </div>
  );
}

function AssistantTurn({ answer }: { answer: Answer }) {
  return (
    <div
      data-testid="ask-answer"
      className="rounded-2xl rounded-bl-sm border border-border bg-card px-4 py-4"
    >
      <AnswerView answer={answer} />
    </div>
  );
}

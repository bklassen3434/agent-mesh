'use client';

import { type FormEvent, useState } from 'react';

import { AnswerView } from '@/components/answer-view';
import { EvidenceProvider } from '@/components/evidence-dialog';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { type Answer, ApiError, api } from '@/lib/api';

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
    <EvidenceProvider field={field.trim() || 'ai-robotics'} role="admin">
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

        {answer && !loading && (
          <Card data-testid="ask-answer">
            <CardContent className="pt-6">
              <AnswerView answer={answer} />
            </CardContent>
          </Card>
        )}
      </div>
    </EvidenceProvider>
  );
}

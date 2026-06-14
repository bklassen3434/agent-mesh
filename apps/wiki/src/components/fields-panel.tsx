'use client';

import { Plus, Settings2 } from 'lucide-react';
import Link from 'next/link';
import { type FormEvent, useState } from 'react';

import { Badge } from '@/components/ui/badge';
import { Button, buttonVariants } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Switch } from '@/components/ui/switch';
import { ApiError, api, type Field } from '@/lib/api';

function parseList(text: string): string[] {
  return text
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean);
}

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 409) return 'A field with that name already exists.';
    if (err.status === 422) return err.message.split('→')[1]?.trim() ?? 'Invalid input.';
  }
  return 'Could not save. Please try again.';
}

export function FieldsPanel({ initialFields }: { initialFields: Field[] }) {
  const [fields, setFields] = useState<Field[]>(initialFields);
  const [creating, setCreating] = useState(false);

  async function refresh() {
    try {
      setFields(await api.listFields());
    } catch {
      /* keep the last good list on a transient read failure */
    }
  }

  return (
    <div className="space-y-6">
      <div>
        {creating ? (
          <FieldForm
            mode="create"
            onDone={async () => {
              setCreating(false);
              await refresh();
            }}
            onCancel={() => setCreating(false)}
          />
        ) : (
          <Button onClick={() => setCreating(true)} size="sm">
            <Plus className="mr-1.5 h-4 w-4" />
            New field
          </Button>
        )}
      </div>

      <div className="space-y-3">
        {fields.length === 0 && (
          <div className="rounded-lg border border-border bg-muted/30 px-4 py-6 text-sm text-muted-foreground">
            No fields yet. Create one to get started.
          </div>
        )}
        {fields.map((f) => (
          <FieldCard key={f.id} field={f} onChanged={refresh} />
        ))}
      </div>
    </div>
  );
}

function FieldCard({ field, onChanged }: { field: Field; onChanged: () => void }) {
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggleActive(next: boolean) {
    setBusy(true);
    setError(null);
    try {
      await api.updateField(field.slug, { is_active: next });
      await onChanged();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className={field.is_active ? undefined : 'opacity-70'}>
      <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
        <div className="space-y-1">
          <CardTitle className="flex items-center gap-2 text-base">
            {field.name}
            <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
              {field.slug}
            </code>
            {field.is_active ? (
              <Badge variant="default">Active</Badge>
            ) : (
              <Badge variant="secondary">Archived</Badge>
            )}
          </CardTitle>
          <p className="text-sm text-muted-foreground">{field.profile.description}</p>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">Active</span>
          <Switch
            checked={field.is_active}
            onCheckedChange={toggleActive}
            disabled={busy}
            aria-label={`${field.name} active`}
          />
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {field.profile.entity_type_hints && field.profile.entity_type_hints.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {field.profile.entity_type_hints.map((h) => (
              <span key={h} className="rounded-full border border-border px-2 py-0.5 text-xs">
                {h}
              </span>
            ))}
          </div>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <Link
            href={`/connectors?field=${encodeURIComponent(field.slug)}`}
            className={buttonVariants({ variant: 'outline', size: 'sm' })}
          >
            <Settings2 className="mr-1.5 h-3.5 w-3.5" />
            Configure connectors
          </Link>
          <Button size="sm" variant="ghost" onClick={() => setEditing((e) => !e)}>
            {editing ? 'Close' : 'Edit profile'}
          </Button>
        </div>
        {error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
            {error}
          </div>
        )}
        {editing && (
          <FieldForm
            mode="edit"
            field={field}
            onDone={async () => {
              setEditing(false);
              await onChanged();
            }}
            onCancel={() => setEditing(false)}
          />
        )}
      </CardContent>
    </Card>
  );
}

function FieldForm({
  mode,
  field,
  onDone,
  onCancel,
}: {
  mode: 'create' | 'edit';
  field?: Field;
  onDone: () => void | Promise<void>;
  onCancel: () => void;
}) {
  const p = field?.profile;
  const [name, setName] = useState(field?.name ?? '');
  const [description, setDescription] = useState(p?.description ?? '');
  const [hints, setHints] = useState((p?.entity_type_hints ?? []).join('\n'));
  const [topicLabel, setTopicLabel] = useState(p?.topic_label ?? 'sota');
  const [examples, setExamples] = useState(p?.extraction_examples ?? '');
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (saving) return;
    setSaving(true);
    setError(null);
    try {
      if (mode === 'create') {
        await api.createField({
          name: name.trim(),
          description: description.trim(),
          entity_type_hints: parseList(hints),
          extraction_examples: examples,
          topic_label: topicLabel.trim() || 'sota',
        });
      } else if (field) {
        await api.updateField(field.slug, {
          name: name.trim(),
          description: description.trim(),
          entity_type_hints: parseList(hints),
          extraction_examples: examples,
          topic_label: topicLabel.trim() || 'sota',
        });
      }
      await onDone();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  const inputCls =
    'w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring';

  return (
    <form
      onSubmit={submit}
      className="space-y-4 rounded-lg border border-border bg-muted/20 p-4"
    >
      {mode === 'create' && (
        <div className="space-y-1">
          <label className="text-xs font-medium" htmlFor="field-name">
            Name
          </label>
          <input
            id="field-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Materials Science"
            className={inputCls}
          />
          <p className="text-xs text-muted-foreground">
            The slug (used everywhere) is derived from the name and can&apos;t change later.
          </p>
        </div>
      )}
      {mode === 'edit' && (
        <div className="space-y-1">
          <label className="text-xs font-medium" htmlFor="field-name">
            Name
          </label>
          <input
            id="field-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className={inputCls}
          />
        </div>
      )}

      <div className="space-y-1">
        <label className="text-xs font-medium" htmlFor="field-desc">
          Description
        </label>
        <input
          id="field-desc"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="a materials-science research knowledge base"
          className={inputCls}
        />
        <p className="text-xs text-muted-foreground">
          A noun phrase — it&apos;s templated into the agent prompts as
          &ldquo;You are a skeptic in <em>…</em>.&rdquo;
        </p>
      </div>

      <div className="space-y-1">
        <label className="text-xs font-medium" htmlFor="field-hints">
          Entity name hints
        </label>
        <textarea
          id="field-hints"
          rows={3}
          value={hints}
          onChange={(e) => setHints(e.target.value)}
          placeholder={'One per line, e.g.\nMOF-5\nPerovskite'}
          className={`${inputCls} resize-y`}
        />
        <p className="text-xs text-muted-foreground">
          Example canonical entity names, shown to the extractor. One per line.
        </p>
      </div>

      <div className="space-y-1">
        <label className="text-xs font-medium" htmlFor="field-topic">
          Topic label
        </label>
        <input
          id="field-topic"
          value={topicLabel}
          onChange={(e) => setTopicLabel(e.target.value)}
          className={`${inputCls} max-w-xs`}
        />
      </div>

      <div>
        <button
          type="button"
          onClick={() => setShowAdvanced((s) => !s)}
          className="text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
        >
          {showAdvanced ? 'Hide' : 'Show'} few-shot examples (advanced)
        </button>
        {showAdvanced && (
          <div className="mt-2 space-y-1">
            <textarea
              rows={6}
              value={examples}
              onChange={(e) => setExamples(e.target.value)}
              placeholder="Optional verbatim extraction examples inserted into the prompt."
              className={`${inputCls} resize-y font-mono text-xs`}
            />
          </div>
        )}
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          {error}
        </div>
      )}

      <div className="flex items-center gap-3">
        <Button type="submit" size="sm" disabled={saving || !name.trim() || !description.trim()}>
          {saving ? 'Saving…' : mode === 'create' ? 'Create field' : 'Save'}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

'use client';

import { ChevronDown, ChevronRight } from 'lucide-react';
import { useState } from 'react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Switch } from '@/components/ui/switch';
import {
  ApiError,
  api,
  type Connector,
  type FieldConnector,
} from '@/lib/api';

// The catalog's config_schema is JSON-schema-lite: each key maps to a spec the
// backend validates against on save. Narrow the generated `unknown` to this.
type FieldType = 'str' | 'int' | 'list[str]' | 'list[dict]';
interface FieldSpec {
  type?: FieldType;
  required?: boolean;
  description?: string;
}

function specOf(raw: unknown): FieldSpec {
  return (raw && typeof raw === 'object' ? (raw as FieldSpec) : {}) ?? {};
}

// ── config ⇄ form-string conversion ──────────────────────────────────────────
// The form holds every value as a string; we convert to the typed JSON the API
// expects on save, and back to a display string from stored config.

function toFormString(value: unknown, type: FieldType): string {
  if (value === undefined || value === null) return '';
  if (type === 'list[str]') {
    return Array.isArray(value) ? value.join('\n') : String(value);
  }
  if (type === 'list[dict]') {
    return JSON.stringify(value, null, 2);
  }
  return String(value);
}

function fromFormString(text: string, type: FieldType): unknown {
  const t = text.trim();
  if (type === 'list[str]') {
    return text
      .split(/[\n,]/)
      .map((s) => s.trim())
      .filter(Boolean);
  }
  if (type === 'int') {
    return t === '' ? undefined : Number(t);
  }
  if (type === 'list[dict]') {
    return t === '' ? [] : (JSON.parse(t) as unknown); // may throw → caught on save
  }
  return t;
}

export function ConnectorsPanel({
  field,
  catalog,
  initialEnablement,
}: {
  field: string;
  catalog: Connector[];
  initialEnablement: FieldConnector[];
}) {
  const byId = new Map(initialEnablement.map((fc) => [fc.connector_id, fc]));

  if (catalog.length === 0) {
    return (
      <div className="rounded-lg border border-border bg-muted/30 px-4 py-6 text-sm text-muted-foreground">
        The connector catalog is empty or the API is unavailable.
      </div>
    );
  }

  const builtin = catalog.filter((c) => c.kind === 'builtin');
  const configDriven = catalog.filter((c) => c.kind !== 'builtin');

  return (
    <div className="space-y-6">
      <Section
        title="Built-in sources"
        hint="The shipped scouts. Toggle which ones this field ingests from."
        connectors={builtin}
        byId={byId}
        field={field}
      />
      <Section
        title="Add a source"
        hint="Generic connectors — point them at a feed, search, or JSON API and enable."
        connectors={configDriven}
        byId={byId}
        field={field}
      />
    </div>
  );
}

function Section({
  title,
  hint,
  connectors,
  byId,
  field,
}: {
  title: string;
  hint: string;
  connectors: Connector[];
  byId: Map<string, FieldConnector>;
  field: string;
}) {
  if (connectors.length === 0) return null;
  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-sm font-semibold">{title}</h2>
        <p className="text-xs text-muted-foreground">{hint}</p>
      </div>
      <div className="space-y-3">
        {connectors.map((c) => (
          <ConnectorCard key={c.id} connector={c} current={byId.get(c.id)} field={field} />
        ))}
      </div>
    </section>
  );
}

function ConnectorCard({
  connector,
  current,
  field,
}: {
  connector: Connector;
  current: FieldConnector | undefined;
  field: string;
}) {
  const schema = (connector.config_schema ?? {}) as Record<string, unknown>;
  const keys = Object.keys(schema);
  const hasConfig = keys.length > 0;

  const [enabled, setEnabled] = useState(current?.enabled ?? false);
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<Record<string, string>>(() => {
    const cfg = (current?.config ?? {}) as Record<string, unknown>;
    const out: Record<string, string> = {};
    for (const k of keys) out[k] = toFormString(cfg[k], specOf(schema[k]).type ?? 'str');
    return out;
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  async function persist(nextEnabled: boolean) {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const config: Record<string, unknown> = {};
      for (const k of keys) {
        const spec = specOf(schema[k]);
        const v = fromFormString(values[k] ?? '', spec.type ?? 'str');
        // Drop empty optionals; keep empties for required so the API returns the
        // precise validation error.
        if (v === undefined || (Array.isArray(v) && v.length === 0 && !spec.required)) {
          continue;
        }
        if (typeof v === 'string' && v === '' && !spec.required) continue;
        config[k] = v;
      }
      await api.updateFieldConnector(field, connector.id, { config, enabled: nextEnabled });
      setEnabled(nextEnabled);
      setSaved(true);
    } catch (err) {
      if (err instanceof SyntaxError) {
        setError('Invalid JSON in one of the fields.');
      } else if (err instanceof ApiError) {
        // The API's 422 detail carries the exact schema-validation message.
        const detail = err.message.split('→')[1]?.trim() ?? err.message;
        setError(err.status === 422 ? detail : 'Could not save. Please try again.');
      } else {
        setError('Could not save. Please try again.');
      }
      // Re-sync the toggle to its last persisted state on failure.
      setEnabled(current?.enabled ?? enabled);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
        <div className="space-y-1">
          <CardTitle className="flex items-center gap-2 text-base">
            {connector.name}
            {enabled ? (
              <Badge variant="default">Enabled</Badge>
            ) : (
              <Badge variant="secondary">Off</Badge>
            )}
          </CardTitle>
          <p className="text-sm text-muted-foreground">{connector.description}</p>
        </div>
        <Switch
          checked={enabled}
          onCheckedChange={(v) => persist(v)}
          disabled={saving}
          aria-label={`${connector.name} enabled`}
        />
      </CardHeader>
      <CardContent className="space-y-3">
        {hasConfig && (
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            className="inline-flex items-center gap-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
          >
            {open ? (
              <ChevronDown className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )}
            Configuration
          </button>
        )}

        {hasConfig && open && (
          <div className="space-y-4 rounded-md border border-border bg-muted/20 p-4">
            {keys.map((k) => {
              const spec = specOf(schema[k]);
              const type = spec.type ?? 'str';
              const id = `${connector.id}-${k}`;
              return (
                <div key={k} className="space-y-1">
                  <label htmlFor={id} className="flex items-center gap-2 text-xs font-medium">
                    {k}
                    {spec.required && <span className="text-destructive">required</span>}
                    <span className="font-normal text-muted-foreground">{type}</span>
                  </label>
                  {spec.description && (
                    <p className="text-xs text-muted-foreground">{spec.description}</p>
                  )}
                  {type === 'list[str]' || type === 'list[dict]' ? (
                    <textarea
                      id={id}
                      rows={type === 'list[dict]' ? 5 : 3}
                      value={values[k] ?? ''}
                      onChange={(e) => setValues((v) => ({ ...v, [k]: e.target.value }))}
                      placeholder={type === 'list[str]' ? 'One per line' : '[ { … } ]'}
                      className="w-full resize-y rounded-md border border-border bg-background px-2 py-1.5 font-mono text-xs outline-none focus:ring-2 focus:ring-ring"
                    />
                  ) : (
                    <input
                      id={id}
                      type={type === 'int' ? 'number' : 'text'}
                      value={values[k] ?? ''}
                      onChange={(e) => setValues((v) => ({ ...v, [k]: e.target.value }))}
                      className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none focus:ring-2 focus:ring-ring"
                    />
                  )}
                </div>
              );
            })}
            <div className="flex items-center gap-3">
              <Button size="sm" onClick={() => persist(enabled)} disabled={saving}>
                {saving ? 'Saving…' : 'Save config'}
              </Button>
              {saved && <span className="text-xs text-muted-foreground">Saved.</span>}
            </div>
          </div>
        )}

        {error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
            {error}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

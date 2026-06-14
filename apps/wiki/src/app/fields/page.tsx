import { FieldsPanel } from '@/components/fields-panel';
import { api, type Field } from '@/lib/api';

export const dynamic = 'force-dynamic';

export default async function FieldsPage() {
  const fields = await api.listFields().catch((): Field[] => []);

  return (
    <main className="space-y-6">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Fields</h1>
        <p className="text-sm text-muted-foreground">
          A field is an isolated knowledge scope with its own entities, claims,
          beliefs, and sources. Create one, give it a profile that frames the
          domain for the extractor, then enable the connectors it ingests from.
        </p>
      </header>
      <FieldsPanel initialFields={fields} />
    </main>
  );
}

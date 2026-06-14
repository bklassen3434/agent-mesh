import { ConnectorsPanel } from '@/components/connectors-panel';
import { api, type Connector, type FieldConnector } from '@/lib/api';

export const dynamic = 'force-dynamic';

type SP = { [k: string]: string | string[] | undefined };

function pick(sp: SP, key: string): string | undefined {
  const v = sp[key];
  return Array.isArray(v) ? v[0] : v;
}

export default async function ConnectorsPage(props: { searchParams: Promise<SP> }) {
  const sp = await props.searchParams;
  const field = pick(sp, 'field') ?? 'ai-robotics';

  // Each source degrades on its own: a missing catalog or per-field row must not
  // blank the page.
  const [catalog, enablement] = await Promise.all([
    api.connectors().catch((): Connector[] => []),
    api.fieldConnectors(field).catch((): FieldConnector[] => []),
  ]);

  return (
    <main className="space-y-6">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Connectors</h1>
        <p className="text-sm text-muted-foreground">
          Choose which sources the <span className="font-medium">{field}</span> field
          ingests from, and configure each one. Changes apply on the next pipeline
          run. Config is validated against each connector&apos;s schema on save.
        </p>
      </header>
      <ConnectorsPanel field={field} catalog={catalog} initialEnablement={enablement} />
    </main>
  );
}

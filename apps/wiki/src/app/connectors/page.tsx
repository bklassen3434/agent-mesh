import { ConnectorsPanel } from '@/components/connectors-panel';
import { api, type Connector, type FieldConnector } from '@/lib/api';
import { getField, getRole } from '@/lib/auth-server';

export const dynamic = 'force-dynamic';

type SP = { [k: string]: string | string[] | undefined };

function pick(sp: SP, key: string): string | undefined {
  const v = sp[key];
  return Array.isArray(v) ? v[0] : v;
}

export default async function ConnectorsPage(props: { searchParams: Promise<SP> }) {
  const sp = await props.searchParams;
  // The global topic dropdown (field cookie) drives the scope; an explicit
  // ?field= still wins for deep links.
  const [role, cookieField] = await Promise.all([getRole(), getField()]);
  const field = pick(sp, 'field') ?? cookieField;
  const readOnly = role !== 'admin';

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
          {readOnly ? (
            <>
              The sources the <span className="font-medium">{field}</span> topic ingests
              from. Sign in as admin to change them.
            </>
          ) : (
            <>
              Choose which sources the <span className="font-medium">{field}</span> topic
              ingests from, and configure each one. Changes apply on the next pipeline run.
              Config is validated against each connector&apos;s schema on save.
            </>
          )}
        </p>
      </header>
      <ConnectorsPanel
        field={field}
        catalog={catalog}
        initialEnablement={enablement}
        readOnly={readOnly}
      />
    </main>
  );
}

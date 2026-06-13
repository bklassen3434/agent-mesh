import { AskPanel } from '@/components/ask-panel';

export const dynamic = 'force-dynamic';

type SP = { [k: string]: string | string[] | undefined };

function pick(sp: SP, key: string): string | undefined {
  const v = sp[key];
  return Array.isArray(v) ? v[0] : v;
}

export default async function AskPage(props: { searchParams: Promise<SP> }) {
  const sp = await props.searchParams;
  const field = pick(sp, 'field') ?? 'ai-robotics';

  return (
    <main className="space-y-6">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold tracking-tight">Ask</h1>
        <p className="text-sm text-muted-foreground">
          Ask a question about this field and get a grounded answer synthesized
          strictly from the mesh&apos;s beliefs, claims, and entities — with
          citations you can follow and an honest coverage signal. The assistant
          says when the mesh has no evidence rather than guessing.
        </p>
      </header>
      <AskPanel initialField={field} />
    </main>
  );
}

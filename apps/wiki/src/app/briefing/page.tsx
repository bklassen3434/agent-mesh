import Link from 'next/link';

import { EmptyState } from '@/components/empty-state';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { ApiError, api } from '@/lib/api';
import type { Briefing, PersonalizedItem } from '@/lib/api';
import { formatConfidence } from '@/lib/format';

export const dynamic = 'force-dynamic';

type SP = { [k: string]: string | string[] | undefined };

function pick(sp: SP, key: string): string | undefined {
  const v = sp[key];
  return Array.isArray(v) ? v[0] : v;
}

export default async function BriefingPage(props: { searchParams: Promise<SP> }) {
  const sp = await props.searchParams;
  const dateParam = pick(sp, 'date');

  let briefing: Briefing | null = null;
  let missingProfile = false;
  let errorMessage: string | null = null;

  try {
    briefing = await api.briefing(dateParam);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      missingProfile = true;
    } else if (e instanceof ApiError) {
      errorMessage = e.message;
    } else {
      throw e;
    }
  }

  return (
    <main className="space-y-6">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">Daily briefing</h1>
        <p className="text-sm text-muted-foreground">
          The Personalizer ranks the last 24 hours of mesh activity against your
          profile and tells you what&apos;s worth your attention.
        </p>
        {briefing?.profile_excerpt ? (
          <p className="text-xs text-muted-foreground italic">
            Based on your profile: &ldquo;{briefing.profile_excerpt}&rdquo;
          </p>
        ) : null}
      </header>

      {missingProfile ? <MissingProfileEmptyState /> : null}

      {errorMessage ? (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 p-4 text-sm">
          <p className="font-medium text-destructive">Briefing unavailable</p>
          <p className="mt-1 text-muted-foreground">{errorMessage}</p>
        </div>
      ) : null}

      {briefing ? <BriefingBody briefing={briefing} /> : null}
    </main>
  );
}

function BriefingBody({ briefing }: { briefing: Briefing }) {
  if (!briefing.sections || briefing.sections.length === 0) {
    return (
      <EmptyState
        title="Quiet day"
        description="No relevant items in the window. Try a different date or check back later."
      />
    );
  }
  return (
    <div className="space-y-8">
      {briefing.sections.map((section, idx) => (
        <section key={`${section.name}-${idx}`} className="space-y-3">
          <div>
            <h2 className="text-lg font-semibold">{section.name}</h2>
            {section.description ? (
              <p className="text-sm text-muted-foreground">{section.description}</p>
            ) : null}
          </div>
          {(section.items?.length ?? 0) === 0 ? (
            <p className="text-sm text-muted-foreground">Nothing surfaced here today.</p>
          ) : (
            <ul className="space-y-3">
              {(section.items ?? []).map((item) => (
                <li key={`${item.item_type}-${item.item_id}`}>
                  <BriefingItemCard item={item} />
                </li>
              ))}
            </ul>
          )}
        </section>
      ))}
    </div>
  );
}

function BriefingItemCard({ item }: { item: PersonalizedItem }) {
  const href = itemHref(item);
  return (
    <Card>
      <CardContent className="space-y-2 pt-6">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Badge variant="secondary">{item.item_type}</Badge>
            <span className="font-mono text-xs text-muted-foreground">{item.item_id}</span>
          </div>
          <Badge variant="outline">relevance {formatConfidence(item.relevance_score)}</Badge>
        </div>
        <p className="text-sm">{item.rationale}</p>
        {href ? (
          <Link href={href} className="text-xs text-primary hover:underline">
            View {item.item_type} →
          </Link>
        ) : null}
      </CardContent>
    </Card>
  );
}

function itemHref(item: PersonalizedItem): string | null {
  switch (item.item_type) {
    case 'belief':
      return `/beliefs/${encodeURIComponent(item.item_id)}`;
    case 'claim':
      return `/claims/${encodeURIComponent(item.item_id)}`;
    case 'revision':
      // Revisions don't have a standalone route; link to the skeptic feed.
      return '/skeptic';
    default:
      return null;
  }
}

function MissingProfileEmptyState() {
  return (
    <EmptyState
      title="No profile configured"
      description={
        <>
          The briefing personalizes against a free-form markdown profile at{' '}
          <span className="font-mono">~/.config/agent_mesh/profile.md</span>. Create
          one with a short description of what you care about (e.g., the topics,
          benchmarks, or research lines you follow). See{' '}
          <span className="font-mono">docs/personalization.md</span> for a starter
          template.
        </>
      }
    />
  );
}

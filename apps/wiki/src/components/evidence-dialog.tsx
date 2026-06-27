'use client';

import { ArrowLeft, ExternalLink } from 'lucide-react';
import Link from 'next/link';
import { createContext, useCallback, useContext, useEffect, useState } from 'react';

import { Badge } from '@/components/ui/badge';
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog';
import {
  type BeliefDetail,
  type Claim,
  type ClaimDetail,
  type EntityDetail,
  type SourceDetail,
  api,
} from '@/lib/api';
import { formatConfidence, formatDateTime } from '@/lib/format';

export type CitationKind = 'belief' | 'claim' | 'entity' | 'source';

type Ref = { kind: CitationKind; id: string };

const KIND_SEGMENT: Record<CitationKind, string> = {
  belief: 'beliefs',
  claim: 'claims',
  entity: 'entities',
  source: 'sources',
};

const KIND_LABEL: Record<CitationKind, string> = {
  belief: 'Belief',
  claim: 'Claim',
  entity: 'Entity',
  source: 'Source',
};

interface EvidenceCtx {
  open: (kind: CitationKind, id: string) => void;
}

const Ctx = createContext<EvidenceCtx | null>(null);

export function useEvidence(): EvidenceCtx {
  const v = useContext(Ctx);
  // Outside a provider (shouldn't happen), fail soft to a no-op.
  return v ?? { open: () => {} };
}

/**
 * Wrap any chat/answer UI in this provider so inline citations and citation
 * pills can open an in-place evidence popup (belief / claim / source / entity)
 * instead of navigating away — keeping beta visitors inside the chatbot. The
 * popup supports drill-down (belief → claim → source) with a back stack.
 */
export function EvidenceProvider({
  field,
  role,
  children,
}: {
  field: string;
  role?: string;
  children: React.ReactNode;
}) {
  const [stack, setStack] = useState<Ref[]>([]);
  const current = stack.length > 0 ? stack[stack.length - 1] : null;

  const open = useCallback((kind: CitationKind, id: string) => {
    setStack([{ kind, id }]);
  }, []);
  const navigate = useCallback((kind: CitationKind, id: string | undefined) => {
    if (!id) return;
    setStack((s) => [...s, { kind, id }]);
  }, []);
  const back = useCallback(() => setStack((s) => s.slice(0, -1)), []);

  return (
    <Ctx.Provider value={{ open }}>
      {children}
      <Dialog
        open={current !== null}
        onOpenChange={(o) => {
          if (!o) setStack([]);
        }}
      >
        {current && (
          <DialogContent>
            <EvidenceBody
              target={current}
              field={field}
              role={role}
              canGoBack={stack.length > 1}
              onBack={back}
              onNavigate={navigate}
            />
          </DialogContent>
        )}
      </Dialog>
    </Ctx.Provider>
  );
}

/** Inline `[kind]` citation rendered within answer prose. */
export function CitationChip({ kind, id }: { kind: CitationKind; id: string }) {
  const { open } = useEvidence();
  return (
    <button
      type="button"
      onClick={() => open(kind, id)}
      className="mx-0.5 rounded bg-muted px-1 align-baseline text-xs font-medium text-foreground hover:bg-accent"
    >
      [{kind}]
    </button>
  );
}

/** A citation pill in the answer's evidence list. */
export function CitationPill({
  kind,
  id,
  quote,
}: {
  kind: CitationKind;
  id: string;
  quote?: string;
}) {
  const { open } = useEvidence();
  return (
    <button
      type="button"
      onClick={() => open(kind, id)}
      title={quote}
      className="inline-flex items-center gap-1 rounded-full border border-border px-2.5 py-0.5 text-xs hover:bg-accent"
    >
      <span className="font-medium">{kind}</span>
      <span className="text-muted-foreground">
        {id.length > 10 ? `${id.slice(0, 8)}…` : id}
      </span>
    </button>
  );
}

// --- popup body -----------------------------------------------------------

type Loaded =
  | { kind: 'belief'; data: BeliefDetail }
  | { kind: 'claim'; data: ClaimDetail }
  | { kind: 'source'; data: SourceDetail }
  | { kind: 'entity'; data: EntityDetail };

function EvidenceBody({
  target,
  field,
  role,
  canGoBack,
  onBack,
  onNavigate,
}: {
  target: Ref;
  field: string;
  role?: string;
  canGoBack: boolean;
  onBack: () => void;
  onNavigate: (kind: CitationKind, id: string | undefined) => void;
}) {
  const [state, setState] = useState<
    { status: 'loading' } | { status: 'error' } | { status: 'ok'; loaded: Loaded }
  >({ status: 'loading' });

  useEffect(() => {
    let alive = true;
    setState({ status: 'loading' });
    (async () => {
      try {
        let loaded: Loaded;
        if (target.kind === 'belief') loaded = { kind: 'belief', data: await api.belief(target.id, field) };
        else if (target.kind === 'claim') loaded = { kind: 'claim', data: await api.claim(target.id, field) };
        else if (target.kind === 'source') loaded = { kind: 'source', data: await api.source(target.id, field) };
        else loaded = { kind: 'entity', data: await api.entity(target.id, field) };
        if (alive) setState({ status: 'ok', loaded });
      } catch {
        if (alive) setState({ status: 'error' });
      }
    })();
    return () => {
      alive = false;
    };
  }, [target.kind, target.id, field]);

  return (
    <>
      <div className="flex items-center gap-2 pr-8">
        {canGoBack && (
          <button
            type="button"
            onClick={onBack}
            aria-label="Back"
            className="rounded-sm text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft className="h-4 w-4" />
          </button>
        )}
        <Badge variant="outline" className="uppercase">
          {KIND_LABEL[target.kind]}
        </Badge>
        {role !== 'beta' && (
          <Link
            href={`/knowledge/${KIND_SEGMENT[target.kind]}/${encodeURIComponent(target.id)}`}
            className="ml-auto inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            Full page <ExternalLink className="h-3 w-3" />
          </Link>
        )}
      </div>

      <div data-testid="evidence-body" className="min-h-[6rem] overflow-y-auto pr-1">
        {state.status === 'loading' && (
          <div role="status" className="h-24 animate-pulse rounded-md bg-muted/40" />
        )}
        {state.status === 'error' && (
          <p className="text-sm text-muted-foreground">Could not load this evidence.</p>
        )}
        {state.status === 'ok' && <Detail loaded={state.loaded} onNavigate={onNavigate} />}
      </div>
    </>
  );
}

function Detail({
  loaded,
  onNavigate,
}: {
  loaded: Loaded;
  onNavigate: (kind: CitationKind, id: string | undefined) => void;
}) {
  if (loaded.kind === 'belief') return <BeliefView detail={loaded.data} onNavigate={onNavigate} />;
  if (loaded.kind === 'claim') return <ClaimView detail={loaded.data} onNavigate={onNavigate} />;
  if (loaded.kind === 'source') return <SourceView detail={loaded.data} onNavigate={onNavigate} />;
  return <EntityView detail={loaded.data} onNavigate={onNavigate} />;
}

function RefButton({
  kind,
  id,
  label,
  onNavigate,
}: {
  kind: CitationKind;
  id: string | undefined;
  label: string;
  onNavigate: (kind: CitationKind, id: string | undefined) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onNavigate(kind, id)}
      className="rounded-md border border-border px-2 py-0.5 text-left text-xs hover:bg-accent"
    >
      {label}
    </button>
  );
}

function ClaimRow({
  claim,
  onNavigate,
}: {
  claim: Claim;
  onNavigate: (kind: CitationKind, id: string | undefined) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onNavigate('claim', claim.id)}
      className="block w-full rounded-md border border-border p-3 text-left hover:bg-accent/50"
    >
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Badge variant="outline" className="font-mono">
          {claim.predicate}
        </Badge>
        <Badge variant="secondary">{formatConfidence(claim.confidence)}</Badge>
      </div>
      <p className="mt-2 line-clamp-3 text-xs text-muted-foreground">{claim.raw_excerpt}</p>
    </button>
  );
}

function BeliefView({
  detail,
  onNavigate,
}: {
  detail: BeliefDetail;
  onNavigate: (kind: CitationKind, id: string | undefined) => void;
}) {
  const { belief, supporting_claims, contradicting_claims } = detail;
  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <div className="text-xs uppercase tracking-wide text-muted-foreground">{belief.topic}</div>
        <DialogTitle>{belief.statement}</DialogTitle>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <Badge variant={belief.is_currently_held ? 'secondary' : 'outline'}>
            {belief.is_currently_held ? 'currently held' : 'no longer held'}
          </Badge>
          <Badge variant="secondary">{formatConfidence(belief.confidence)} confidence</Badge>
          <span className="text-muted-foreground">revision {belief.revision_count}</span>
        </div>
      </div>
      <Section title={`Supporting claims (${supporting_claims.length})`}>
        {supporting_claims.length === 0 ? (
          <Empty />
        ) : (
          supporting_claims.map((c) => (
            <ClaimRow key={c.claim.id} claim={c.claim} onNavigate={onNavigate} />
          ))
        )}
      </Section>
      {contradicting_claims.length > 0 && (
        <Section title={`Contradicting claims (${contradicting_claims.length})`}>
          {contradicting_claims.map((c) => (
            <ClaimRow key={c.claim.id} claim={c.claim} onNavigate={onNavigate} />
          ))}
        </Section>
      )}
    </div>
  );
}

function ClaimView({
  detail,
  onNavigate,
}: {
  detail: ClaimDetail;
  onNavigate: (kind: CitationKind, id: string | undefined) => void;
}) {
  const { claim, source, subject_entity } = detail;
  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <Badge variant="outline" className="font-mono">
            {claim.predicate}
          </Badge>
          <Badge variant="secondary">{formatConfidence(claim.confidence)}</Badge>
          <Badge variant={claim.status === 'active' ? 'secondary' : 'outline'}>{claim.status}</Badge>
        </div>
        <DialogTitle>
          {subject_entity ? subject_entity.canonical_name : 'Unknown subject'}{' '}
          <span className="font-normal text-muted-foreground">— {claim.predicate}</span>
        </DialogTitle>
      </div>
      <Section title="Excerpt">
        <pre className="whitespace-pre-wrap break-words rounded-md border border-border p-3 font-sans text-sm">
          {claim.raw_excerpt}
        </pre>
      </Section>
      {(subject_entity || source) && (
        <div className="flex flex-wrap gap-2">
          {subject_entity && (
            <RefButton
              kind="entity"
              id={subject_entity.id}
              label={`About ${subject_entity.canonical_name}`}
              onNavigate={onNavigate}
            />
          )}
          {source && (
            <RefButton kind="source" id={source.id} label="View source" onNavigate={onNavigate} />
          )}
        </div>
      )}
    </div>
  );
}

function SourceView({
  detail,
  onNavigate,
}: {
  detail: SourceDetail;
  onNavigate: (kind: CitationKind, id: string | undefined) => void;
}) {
  const { source, claims } = detail;
  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <Badge variant="secondary">{source.type}</Badge>
          <span className="text-muted-foreground">
            reliability {formatConfidence(source.reliability_prior)}
          </span>
        </div>
        <DialogTitle>
          <a
            href={source.url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 break-all hover:underline"
          >
            {source.url} <ExternalLink className="h-3 w-3 shrink-0" />
          </a>
        </DialogTitle>
        <p className="text-xs text-muted-foreground">
          {source.author ? <>by {source.author} · </> : null}
          published {formatDateTime(source.published_at)}
        </p>
      </div>
      <Section title={`Claims from this source (${claims.length})`}>
        {claims.length === 0 ? (
          <Empty />
        ) : (
          claims.map((c) => <ClaimRow key={c.id} claim={c} onNavigate={onNavigate} />)
        )}
      </Section>
    </div>
  );
}

function EntityView({
  detail,
  onNavigate,
}: {
  detail: EntityDetail;
  onNavigate: (kind: CitationKind, id: string | undefined) => void;
}) {
  const { entity, claims } = detail;
  return (
    <div className="space-y-4">
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <DialogTitle>{entity.canonical_name}</DialogTitle>
          <Badge variant="secondary">{entity.type}</Badge>
        </div>
        {entity.aliases?.length ? (
          <p className="text-xs text-muted-foreground">aka {entity.aliases.join(', ')}</p>
        ) : null}
      </div>
      <Section title={`Claims about this entity (${claims.length})`}>
        {claims.length === 0 ? (
          <Empty />
        ) : (
          claims.map((c) => <ClaimRow key={c.id} claim={c} onNavigate={onNavigate} />)
        )}
      </Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-2">
      <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </div>
      <div className="space-y-2">{children}</div>
    </div>
  );
}

function Empty() {
  return <div className="text-sm text-muted-foreground">None.</div>;
}

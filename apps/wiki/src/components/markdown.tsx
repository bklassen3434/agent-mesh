'use client';

import ReactMarkdown, { type Components, defaultUrlTransform } from 'react-markdown';
import remarkGfm from 'remark-gfm';

import { CitationChip, type CitationKind } from '@/components/evidence-dialog';
import { cn } from '@/lib/utils';

const CITATION_KINDS = new Set(['belief', 'claim', 'entity', 'source']);
const CITATION_RE = /\[(belief|claim|entity|source):([^\]\s]+)\]/g;

// Turn the agent's inline `[kind:id]` citation tokens into markdown links with a
// private `mesh:` scheme, so the markdown parser hands them to our custom <a>
// renderer (which swaps in an in-place evidence popup instead of navigating).
function linkifyCitations(md: string): string {
  return md.replace(CITATION_RE, (_full, kind: string, id: string) => `[${kind}](mesh:${kind}:${id})`);
}

function parseMeshHref(href: string): { kind: CitationKind; id: string } | null {
  if (!href.startsWith('mesh:')) return null;
  const rest = href.slice('mesh:'.length);
  const sep = rest.indexOf(':');
  if (sep < 0) return null;
  const kind = rest.slice(0, sep);
  const id = rest.slice(sep + 1);
  if (!CITATION_KINDS.has(kind) || !id) return null;
  return { kind: kind as CitationKind, id };
}

// react-markdown sanitizes link URLs and would strip our private `mesh:` scheme;
// preserve it and defer to the default (XSS-safe) transform for everything else.
function urlTransform(url: string): string {
  return url.startsWith('mesh:') ? url : defaultUrlTransform(url);
}

// react-markdown passes a `node` prop to every component override; it must not
// reach the DOM element, so each renderer drops it.
const COMPONENTS: Components = {
  a({ node: _node, href, children, ...props }) {
    const cite = href ? parseMeshHref(href) : null;
    if (cite) return <CitationChip kind={cite.kind} id={cite.id} />;
    return (
      <a href={href} target="_blank" rel="noreferrer" {...props}>
        {children}
      </a>
    );
  },
  p: ({ node: _node, className, ...p }) => <p className={cn('leading-relaxed', className)} {...p} />,
  ul: ({ node: _node, className, ...p }) => (
    <ul className={cn('list-disc space-y-1 pl-5', className)} {...p} />
  ),
  ol: ({ node: _node, className, ...p }) => (
    <ol className={cn('list-decimal space-y-1 pl-5', className)} {...p} />
  ),
  li: ({ node: _node, className, ...p }) => <li className={cn('leading-relaxed', className)} {...p} />,
  strong: ({ node: _node, className, ...p }) => (
    <strong className={cn('font-semibold text-foreground', className)} {...p} />
  ),
  h1: ({ node: _node, className, ...p }) => (
    <h1 className={cn('text-base font-semibold', className)} {...p} />
  ),
  h2: ({ node: _node, className, ...p }) => (
    <h2 className={cn('text-sm font-semibold', className)} {...p} />
  ),
  h3: ({ node: _node, className, ...p }) => (
    <h3 className={cn('text-sm font-semibold', className)} {...p} />
  ),
  code: ({ node: _node, className, ...p }) => (
    <code className={cn('rounded bg-muted px-1 py-0.5 font-mono text-xs', className)} {...p} />
  ),
  pre: ({ node: _node, className, ...p }) => (
    <pre
      className={cn(
        'overflow-auto rounded-md bg-muted p-3 font-mono text-xs [&_code]:bg-transparent [&_code]:p-0',
        className,
      )}
      {...p}
    />
  ),
  blockquote: ({ node: _node, className, ...p }) => (
    <blockquote
      className={cn('border-l-2 border-border pl-3 text-muted-foreground', className)}
      {...p}
    />
  ),
  table: ({ node: _node, className, ...p }) => (
    <div className="overflow-x-auto">
      <table className={cn('w-full border-collapse text-xs', className)} {...p} />
    </div>
  ),
  th: ({ node: _node, className, ...p }) => (
    <th className={cn('border border-border px-2 py-1 text-left font-medium', className)} {...p} />
  ),
  td: ({ node: _node, className, ...p }) => (
    <td className={cn('border border-border px-2 py-1', className)} {...p} />
  ),
};

/**
 * Render a grounded answer's markdown. Inline `[kind:id]` citations become
 * clickable chips that open the evidence popup; everything else is normal,
 * sanitized markdown (no raw HTML).
 */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="space-y-3 text-sm leading-relaxed">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        urlTransform={urlTransform}
        components={COMPONENTS}
      >
        {linkifyCitations(children)}
      </ReactMarkdown>
    </div>
  );
}

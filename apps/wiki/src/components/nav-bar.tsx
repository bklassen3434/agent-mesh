'use client';

import { Check, ChevronDown, Eye, Menu, Plus } from 'lucide-react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useTransition } from 'react';

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet';
import { FIELD_COOKIE, PREVIEW_COOKIE, type Role } from '@/lib/auth';
import { cn } from '@/lib/utils';

type Topic = { slug: string; name: string };

const KNOWLEDGE = [
  { href: '/knowledge/beliefs', label: 'Beliefs' },
  { href: '/knowledge/entities', label: 'Entities' },
  { href: '/knowledge/claims', label: 'Claims' },
  { href: '/knowledge/sources', label: 'Sources' },
];

// Pages a beta visitor sees; admins additionally get the knowledge base, agents,
// and topic management.
const BETA_LINKS = [
  { href: '/', label: 'Chat' },
  { href: '/overview', label: 'Overview' },
  { href: '/graph', label: 'Graph' },
  { href: '/connectors', label: 'Connectors' },
];
const ADMIN_LINKS = [
  { href: '/', label: 'Chat' },
  { href: '/overview', label: 'Overview' },
  { href: '/briefing', label: 'Daily Brief' },
  { href: '/graph', label: 'Graph' },
  { href: '/connectors', label: 'Connectors' },
  { href: '/agents', label: 'Agents' },
  { href: '/fields', label: 'Topics' },
];

// --- cookie helpers (preview is not security-sensitive: it only downgrades) ---
function setCookie(name: string, value: string, maxAge: number) {
  document.cookie = `${name}=${encodeURIComponent(value)}; path=/; max-age=${maxAge}; samesite=lax`;
}

export function NavBar({
  statusHref,
  effectiveRole,
  realRole,
  isPreviewing,
  field,
  topics,
}: {
  statusHref: string;
  effectiveRole: Role;
  realRole: Role;
  isPreviewing: boolean;
  field: string;
  topics: Topic[];
}) {
  const pathname = usePathname();
  const isAdmin = effectiveRole === 'admin';
  const links = isAdmin ? ADMIN_LINKS : BETA_LINKS;
  const isActive = (href: string) =>
    href === '/' ? pathname === '/' : pathname === href || pathname.startsWith(`${href}/`);
  const knowledgeActive = pathname.startsWith('/knowledge');
  const linkCls = (active: boolean) =>
    cn(
      'transition-colors hover:text-foreground',
      active ? 'font-medium text-foreground' : 'text-muted-foreground',
    );

  return (
    <>
      {isPreviewing && <PreviewBanner />}
      <nav className="border-b border-border bg-card">
        <div className="mx-auto flex max-w-6xl items-center gap-6 px-6 py-3">
          <Link href="/" className="font-semibold tracking-tight">
            Agent Mesh
          </Link>

          {/* desktop nav */}
          <div className="hidden flex-1 items-center gap-5 text-sm md:flex">
            {links.map((l) => (
              <span key={l.href} className="contents">
                <Link href={l.href} className={linkCls(isActive(l.href))}>
                  {l.label}
                </Link>
                {/* admins get the knowledge-base dropdown right after Daily Brief */}
                {isAdmin && l.href === '/briefing' && (
                  <KnowledgeMenu active={knowledgeActive} linkCls={linkCls} />
                )}
              </span>
            ))}

            <div className="ml-auto flex items-center gap-3">
              <TopicSwitcher field={field} topics={topics} isAdmin={isAdmin} />
              {/* Mode control only for a real admin who isn't already previewing. */}
              {realRole === 'admin' && !isPreviewing && <ModeMenu />}
            </div>
          </div>

          {/* mobile drawer */}
          <div className="flex flex-1 justify-end md:hidden">
            <MobileMenu
              links={links}
              isAdmin={isAdmin}
              realRole={realRole}
              isActive={isActive}
              statusHref={statusHref}
              field={field}
              topics={topics}
            />
          </div>
        </div>
      </nav>
    </>
  );
}

function PreviewBanner() {
  const router = useRouter();
  const [, startTransition] = useTransition();
  const exit = () => {
    setCookie(PREVIEW_COOKIE, '', 0);
    startTransition(() => router.refresh());
  };
  return (
    <div className="bg-amber-500/15 text-amber-900 dark:text-amber-200">
      <div className="mx-auto flex max-w-6xl items-center justify-between gap-3 px-6 py-1.5 text-xs">
        <span className="flex items-center gap-1.5">
          <Eye className="h-3.5 w-3.5" />
          You&apos;re previewing what beta visitors see.
        </span>
        <button type="button" onClick={exit} className="font-semibold underline-offset-2 hover:underline">
          Back to admin
        </button>
      </div>
    </div>
  );
}

function ModeMenu() {
  const router = useRouter();
  const [, startTransition] = useTransition();
  const previewBeta = () => {
    setCookie(PREVIEW_COOKIE, 'beta', 60 * 60 * 24);
    startTransition(() => router.refresh());
  };
  return (
    <button
      type="button"
      onClick={previewBeta}
      className="inline-flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-xs font-medium text-muted-foreground outline-none transition-colors hover:bg-accent hover:text-foreground"
    >
      <Eye className="h-3.5 w-3.5" />
      View as beta
    </button>
  );
}

function KnowledgeMenu({
  active,
  linkCls,
}: {
  active: boolean;
  linkCls: (a: boolean) => string;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        className={cn('inline-flex items-center gap-1 outline-none', linkCls(active))}
      >
        Knowledge
        <ChevronDown className="h-3.5 w-3.5" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start">
        {KNOWLEDGE.map((l) => (
          <DropdownMenuItem asChild key={l.href}>
            <Link href={l.href}>{l.label}</Link>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function useSelectTopic() {
  const router = useRouter();
  const [, startTransition] = useTransition();
  return (slug: string) => {
    setCookie(FIELD_COOKIE, slug, 60 * 60 * 24 * 365);
    startTransition(() => router.refresh());
  };
}

function TopicSwitcher({
  field,
  topics,
  isAdmin,
}: {
  field: string;
  topics: Topic[];
  isAdmin: boolean;
}) {
  const select = useSelectTopic();
  const current = topics.find((t) => t.slug === field);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger className="inline-flex items-center gap-1 rounded-md border border-border px-2.5 py-1 text-xs font-medium outline-none hover:bg-accent">
        <span className="text-muted-foreground">Topic:</span>
        <span>{current?.name ?? field}</span>
        <ChevronDown className="h-3.5 w-3.5" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {topics.length === 0 && (
          <DropdownMenuItem disabled>No topics yet</DropdownMenuItem>
        )}
        {topics.map((t) => (
          <DropdownMenuItem key={t.slug} onSelect={() => select(t.slug)}>
            <Check
              className={cn('mr-2 h-3.5 w-3.5', t.slug === field ? 'opacity-100' : 'opacity-0')}
            />
            {t.name}
          </DropdownMenuItem>
        ))}
        {isAdmin && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem asChild>
              <Link href="/fields">
                <Plus className="mr-2 h-3.5 w-3.5" />
                New topic…
              </Link>
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function MobileMenu({
  links,
  isAdmin,
  realRole,
  isActive,
  statusHref,
  field,
  topics,
}: {
  links: { href: string; label: string }[];
  isAdmin: boolean;
  realRole: Role;
  isActive: (href: string) => boolean;
  statusHref: string;
  field: string;
  topics: Topic[];
}) {
  const select = useSelectTopic();
  const router = useRouter();
  return (
    <Sheet>
      <SheetTrigger
        aria-label="Open menu"
        className="text-muted-foreground transition-colors hover:text-foreground"
      >
        <Menu className="h-5 w-5" />
      </SheetTrigger>
      <SheetContent side="left" aria-describedby={undefined}>
        <SheetTitle className="text-sm font-semibold">Agent Mesh</SheetTitle>
        <div className="mt-4 flex flex-col gap-0.5 text-sm">
          {links.map((l) => (
            <DrawerLink key={l.href} href={l.href} label={l.label} active={isActive(l.href)} />
          ))}
          {isAdmin && (
            <>
              <div className="mt-3 px-2 text-xs uppercase tracking-wide text-muted-foreground">
                Knowledge
              </div>
              {KNOWLEDGE.map((l) => (
                <DrawerLink
                  key={l.href}
                  href={l.href}
                  label={l.label}
                  active={isActive(l.href)}
                  indent
                />
              ))}
            </>
          )}

          <div className="mt-3 px-2 text-xs uppercase tracking-wide text-muted-foreground">
            Topic
          </div>
          {topics.map((t) => (
            <SheetClose asChild key={t.slug}>
              <button
                type="button"
                onClick={() => select(t.slug)}
                className={cn(
                  'flex items-center rounded px-2 py-1.5 text-left transition-colors hover:bg-accent',
                  t.slug === field ? 'font-medium text-foreground' : 'text-muted-foreground',
                )}
              >
                <Check
                  className={cn(
                    'mr-2 h-3.5 w-3.5',
                    t.slug === field ? 'opacity-100' : 'opacity-0',
                  )}
                />
                {t.name}
              </button>
            </SheetClose>
          ))}

          {realRole === 'admin' && (
            <>
              <div className="mt-3" />
              <SheetClose asChild>
                <button
                  type="button"
                  onClick={() => {
                    setCookie(PREVIEW_COOKIE, isAdmin ? 'beta' : '', isAdmin ? 60 * 60 * 24 : 0);
                    router.refresh();
                  }}
                  className="rounded px-2 py-1.5 text-left text-muted-foreground transition-colors hover:bg-accent"
                >
                  {isAdmin ? 'View as beta' : 'Back to admin'}
                </button>
              </SheetClose>
            </>
          )}
          <a
            href={statusHref}
            className="mt-3 rounded px-2 py-1.5 font-mono text-xs uppercase tracking-wider text-muted-foreground transition-colors hover:text-foreground"
          >
            mesh status →
          </a>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function DrawerLink({
  href,
  label,
  active,
  indent,
}: {
  href: string;
  label: string;
  active: boolean;
  indent?: boolean;
}) {
  return (
    <SheetClose asChild>
      <Link
        href={href}
        className={cn(
          'rounded px-2 py-1.5 transition-colors hover:bg-accent',
          indent && 'pl-4',
          active ? 'font-medium text-foreground' : 'text-muted-foreground',
        )}
      >
        {label}
      </Link>
    </SheetClose>
  );
}

'use client';

import { Check, ChevronDown, LogOut, Menu, Plus } from 'lucide-react';
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
import { FIELD_COOKIE, type Role } from '@/lib/auth';
import { cn } from '@/lib/utils';

type Topic = { slug: string; name: string };

const KNOWLEDGE = [
  { href: '/knowledge/beliefs', label: 'Beliefs' },
  { href: '/knowledge/entities', label: 'Entities' },
  { href: '/knowledge/claims', label: 'Claims' },
  { href: '/knowledge/sources', label: 'Sources' },
];

// Pages a beta visitor sees; admins additionally get the knowledge base, agents,
// pipelines, and topic management.
const BETA_LINKS = [
  { href: '/', label: 'Chat' },
  { href: '/graph', label: 'Graph' },
  { href: '/connectors', label: 'Connectors' },
];
const ADMIN_LINKS = [
  { href: '/', label: 'Chat' },
  { href: '/briefing', label: 'Daily Brief' },
  { href: '/graph', label: 'Graph' },
  { href: '/connectors', label: 'Connectors' },
  { href: '/agents', label: 'Agents' },
  { href: '/pipelines', label: 'Pipelines' },
  { href: '/fields', label: 'Topics' },
];

export function NavBar({
  statusHref,
  role,
  field,
  topics,
  loginConfigured,
}: {
  statusHref: string;
  role: Role;
  field: string;
  topics: Topic[];
  loginConfigured: boolean;
}) {
  const pathname = usePathname();
  const isAdmin = role === 'admin';
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
            <AuthControl isAdmin={isAdmin} loginConfigured={loginConfigured} />
          </div>
        </div>

        {/* mobile drawer */}
        <div className="flex flex-1 justify-end md:hidden">
          <MobileMenu
            links={links}
            isAdmin={isAdmin}
            isActive={isActive}
            statusHref={statusHref}
            field={field}
            topics={topics}
            loginConfigured={loginConfigured}
          />
        </div>
      </div>
    </nav>
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
    document.cookie = `${FIELD_COOKIE}=${encodeURIComponent(slug)}; path=/; max-age=${
      60 * 60 * 24 * 365
    }; samesite=lax`;
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

function AuthControl({
  isAdmin,
  loginConfigured,
}: {
  isAdmin: boolean;
  loginConfigured: boolean;
}) {
  const router = useRouter();
  if (isAdmin) {
    return (
      <button
        type="button"
        onClick={async () => {
          await fetch('/api/auth/logout', { method: 'POST' });
          router.replace('/');
          router.refresh();
        }}
        className="inline-flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
      >
        <LogOut className="h-3.5 w-3.5" />
        Log out
      </button>
    );
  }
  if (!loginConfigured) return null;
  return (
    <Link
      href="/login"
      className="text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
    >
      Log in
    </Link>
  );
}

function MobileMenu({
  links,
  isAdmin,
  isActive,
  statusHref,
  field,
  topics,
  loginConfigured,
}: {
  links: { href: string; label: string }[];
  isAdmin: boolean;
  isActive: (href: string) => boolean;
  statusHref: string;
  field: string;
  topics: Topic[];
  loginConfigured: boolean;
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

          <div className="mt-3" />
          {isAdmin ? (
            <SheetClose asChild>
              <button
                type="button"
                onClick={async () => {
                  await fetch('/api/auth/logout', { method: 'POST' });
                  router.replace('/');
                  router.refresh();
                }}
                className="rounded px-2 py-1.5 text-left text-muted-foreground transition-colors hover:bg-accent"
              >
                Log out
              </button>
            </SheetClose>
          ) : (
            loginConfigured && <DrawerLink href="/login" label="Log in" active={isActive('/login')} />
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

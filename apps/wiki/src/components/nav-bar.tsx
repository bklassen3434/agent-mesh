'use client';

import { ChevronDown, Menu } from 'lucide-react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet';
import { cn } from '@/lib/utils';

const KNOWLEDGE = [
  { href: '/knowledge/beliefs', label: 'Beliefs' },
  { href: '/knowledge/entities', label: 'Entities' },
  { href: '/knowledge/claims', label: 'Claims' },
  { href: '/knowledge/sources', label: 'Sources' },
];

export function NavBar({ statusHref }: { statusHref: string }) {
  const pathname = usePathname();
  const isActive = (href: string) => pathname === href || pathname.startsWith(href + '/');
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
          <Link href="/briefing" className={linkCls(isActive('/briefing'))}>
            Daily Brief
          </Link>
          <Link href="/ask" className={linkCls(isActive('/ask'))}>
            Ask
          </Link>
          <DropdownMenu>
            <DropdownMenuTrigger
              className={cn('inline-flex items-center gap-1 outline-none', linkCls(knowledgeActive))}
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
          <Link href="/graph" className={linkCls(isActive('/graph'))}>
            Graph
          </Link>
          <Link href="/agents" className={linkCls(isActive('/agents'))}>
            Agents
          </Link>
          <Link href="/fields" className={linkCls(isActive('/fields'))}>
            Fields
          </Link>
          <Link href="/connectors" className={linkCls(isActive('/connectors'))}>
            Connectors
          </Link>
        </div>

        {/* mobile drawer */}
        <div className="flex flex-1 justify-end md:hidden">
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
                <DrawerLink href="/briefing" label="Daily Brief" active={isActive('/briefing')} />
                <DrawerLink href="/ask" label="Ask" active={isActive('/ask')} />
                <div className="mt-3 px-2 text-xs uppercase tracking-wide text-muted-foreground">
                  Knowledge
                </div>
                {KNOWLEDGE.map((l) => (
                  <DrawerLink key={l.href} href={l.href} label={l.label} active={isActive(l.href)} indent />
                ))}
                <div className="mt-3" />
                <DrawerLink href="/graph" label="Graph" active={isActive('/graph')} />
                <DrawerLink href="/agents" label="Agents" active={isActive('/agents')} />
                <DrawerLink href="/fields" label="Fields" active={isActive('/fields')} />
                <DrawerLink href="/connectors" label="Connectors" active={isActive('/connectors')} />
                <a
                  href={statusHref}
                  className="mt-3 rounded px-2 py-1.5 font-mono text-xs uppercase tracking-wider text-muted-foreground transition-colors hover:text-foreground"
                >
                  mesh status →
                </a>
              </div>
            </SheetContent>
          </Sheet>
        </div>

        <a
          href={statusHref}
          className="hidden font-mono text-xs uppercase tracking-wider text-muted-foreground transition-colors hover:text-foreground md:inline"
        >
          mesh status →
        </a>
      </div>
    </nav>
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

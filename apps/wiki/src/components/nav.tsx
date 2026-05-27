import Link from 'next/link';

const links = [
  { href: '/', label: 'Home' },
  { href: '/briefing', label: 'Briefing' },
  { href: '/entities', label: 'Entities' },
  { href: '/beliefs', label: 'Beliefs' },
  { href: '/claims', label: 'Claims' },
  { href: '/sources', label: 'Sources' },
  { href: '/skeptic', label: 'Skeptic' },
];

// Status lives on the API service, not the wiki — link out so the page
// visually feels like an admin surface, not another wiki tab.
const statusHref =
  (process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000') + '/status';

export function Nav() {
  return (
    <nav className="border-b border-border bg-card">
      <div className="mx-auto flex max-w-6xl items-center gap-6 px-6 py-3">
        <Link href="/" className="font-semibold tracking-tight">
          Agent Mesh
        </Link>
        <div className="flex flex-1 items-center gap-4 text-sm text-muted-foreground">
          {links.slice(1).map((l) => (
            <Link
              key={l.href}
              href={l.href}
              className="transition-colors hover:text-foreground"
            >
              {l.label}
            </Link>
          ))}
        </div>
        <a
          href={statusHref}
          className="text-xs font-mono uppercase tracking-wider text-muted-foreground transition-colors hover:text-foreground"
        >
          mesh status →
        </a>
      </div>
    </nav>
  );
}

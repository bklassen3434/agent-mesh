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

export function Nav() {
  return (
    <nav className="border-b border-border bg-card">
      <div className="mx-auto flex max-w-6xl items-center gap-6 px-6 py-3">
        <Link href="/" className="font-semibold tracking-tight">
          Agent Mesh
        </Link>
        <div className="flex items-center gap-4 text-sm text-muted-foreground">
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
      </div>
    </nav>
  );
}

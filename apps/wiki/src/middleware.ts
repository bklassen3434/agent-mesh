import { type NextRequest, NextResponse } from 'next/server';

import { adminAuthConfigured, PREVIEW_COOKIE, ROLE_COOKIE, resolveView } from '@/lib/auth';

// Pages only an admin may see. Beta visitors (and admins previewing as beta) get
// the chat, graph, and (view-only) connectors; everything that exposes the inner
// workings — the knowledge base, agent observability, pipelines, and topic
// management — is gated here and silently redirects home (no hint admin exists).
const ADMIN_PREFIXES = [
  '/knowledge',
  '/agents',
  '/pipelines',
  '/skeptic',
  '/briefing',
  '/fields',
];

export async function middleware(req: NextRequest) {
  // Open mode: no admin token configured → the gate is off (local/dev).
  if (!adminAuthConfigured()) return NextResponse.next();

  const { pathname } = req.nextUrl;
  const needsAdmin = ADMIN_PREFIXES.some(
    (p) => pathname === p || pathname.startsWith(`${p}/`),
  );
  if (!needsAdmin) return NextResponse.next();

  // Gate on the *effective* role so previewing-as-beta faithfully loses access.
  const { effectiveRole } = await resolveView(
    req.cookies.get(ROLE_COOKIE)?.value,
    req.cookies.get(PREVIEW_COOKIE)?.value,
  );
  if (effectiveRole === 'admin') return NextResponse.next();

  const url = req.nextUrl.clone();
  url.pathname = '/';
  url.search = '';
  return NextResponse.redirect(url);
}

export const config = {
  matcher: [
    '/knowledge/:path*',
    '/agents/:path*',
    '/pipelines/:path*',
    '/skeptic/:path*',
    '/briefing/:path*',
    '/fields/:path*',
  ],
};

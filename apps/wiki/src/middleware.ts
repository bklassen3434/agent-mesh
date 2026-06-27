import { type NextRequest, NextResponse } from 'next/server';

import { adminLoginConfigured, ROLE_COOKIE, verifyRoleCookie } from '@/lib/auth';

// Pages only an admin may see. Beta visitors get the chat, graph, and
// (view-only) connectors; everything that exposes the inner workings — the
// knowledge base, agent observability, pipelines, and topic management — is
// gated here and redirects to /login.
const ADMIN_PREFIXES = [
  '/knowledge',
  '/agents',
  '/pipelines',
  '/skeptic',
  '/briefing',
  '/fields',
];

export async function middleware(req: NextRequest) {
  // Open mode: no admin password configured → the role gate is off and the wiki
  // behaves like the pre-auth, fully-visible wiki. Set MESH_ADMIN_PASSWORD to
  // turn beta gating on.
  if (!adminLoginConfigured()) return NextResponse.next();

  const { pathname } = req.nextUrl;
  const needsAdmin = ADMIN_PREFIXES.some(
    (p) => pathname === p || pathname.startsWith(`${p}/`),
  );
  if (!needsAdmin) return NextResponse.next();

  const role = await verifyRoleCookie(req.cookies.get(ROLE_COOKIE)?.value);
  if (role === 'admin') return NextResponse.next();

  const url = req.nextUrl.clone();
  url.pathname = '/login';
  url.searchParams.set('next', pathname);
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

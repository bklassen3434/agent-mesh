import { type NextRequest, NextResponse } from 'next/server';

import { PREVIEW_COOKIE, resolveView } from '@/lib/auth';

// Pages only an admin may see. On a beta-only instance (MESH_ADMIN_MODE off)
// these always redirect home — a public visitor gets the chat, graph, and
// (view-only) connectors and no hint the rest exists. On an admin instance they
// open, except while previewing as beta (gating runs on the effective role).
const ADMIN_PREFIXES = [
  '/knowledge',
  '/agents',
  '/skeptic',
  '/briefing',
  '/fields',
];

export function middleware(req: NextRequest) {
  const { pathname } = req.nextUrl;
  const needsAdmin = ADMIN_PREFIXES.some(
    (p) => pathname === p || pathname.startsWith(`${p}/`),
  );
  if (!needsAdmin) return NextResponse.next();

  const { effectiveRole } = resolveView(req.cookies.get(PREVIEW_COOKIE)?.value);
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
    '/skeptic/:path*',
    '/briefing/:path*',
    '/fields/:path*',
  ],
};

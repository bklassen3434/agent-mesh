// Admin-only write proxy. Every privileged API write (create/patch topic,
// connector toggle, schedule patch, pipeline trigger) goes through here so the
// admin check happens once, server-side — then forwards to the API with the
// shared internal token + admin role. A beta-only instance (or an admin
// previewing as beta) gets a 403 and never reaches the API.
import { type NextRequest, NextResponse } from 'next/server';

import { PREVIEW_COOKIE, resolveView } from '@/lib/auth';
import { internalApiBase, internalToken } from '@/lib/proxy';

async function handle(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  // Effective role: a beta-only instance (or an admin previewing as beta) is
  // denied writes, so the boundary holds and the preview is faithful.
  const { effectiveRole } = resolveView(req.cookies.get(PREVIEW_COOKIE)?.value);
  if (effectiveRole !== 'admin') {
    return NextResponse.json({ detail: 'Admin access required.' }, { status: 403 });
  }

  const { path } = await ctx.params;
  const search = new URL(req.url).search;
  const target = `${internalApiBase()}/api/v1/${path
    .map(encodeURIComponent)
    .join('/')}${search}`;

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-Mesh-Role': 'admin',
  };
  const tok = internalToken();
  if (tok) headers['X-Mesh-Internal-Token'] = tok;

  const apiRes = await fetch(target, {
    method: req.method,
    headers,
    body: await req.text(),
  });
  return new NextResponse(await apiRes.text(), {
    status: apiRes.status,
    headers: { 'Content-Type': apiRes.headers.get('Content-Type') ?? 'application/json' },
  });
}

export const POST = handle;
export const PATCH = handle;
export const PUT = handle;

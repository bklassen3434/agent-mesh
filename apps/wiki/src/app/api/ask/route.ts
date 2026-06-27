// Rate-limited chatbot proxy. The browser asks here (first-party), and the wiki
// forwards to the API with the shared internal token, the viewer's role, and —
// for anonymous beta visitors — a stable per-browser beta id the API counts
// against the daily quota. Admins are forwarded as role=admin (unlimited).
//
//   GET  → remaining quota for this browser
//   POST → ask a grounded question (429 when the day's quota is spent)
import { type NextRequest, NextResponse } from 'next/server';

import { BETA_COOKIE, ROLE_COOKIE, resolveRole } from '@/lib/auth';
import { internalApiBase, internalToken } from '@/lib/proxy';

const ONE_YEAR = 60 * 60 * 24 * 365;

function roleOf(req: NextRequest): Promise<'admin' | 'beta'> {
  return resolveRole(req.cookies.get(ROLE_COOKIE)?.value);
}

function ensureBetaId(req: NextRequest): { id: string; isNew: boolean } {
  const existing = req.cookies.get(BETA_COOKIE)?.value;
  if (existing) return { id: existing, isNew: false };
  return { id: crypto.randomUUID(), isNew: true };
}

function setBetaCookie(res: NextResponse, id: string) {
  res.cookies.set(BETA_COOKIE, id, {
    httpOnly: true,
    sameSite: 'lax',
    path: '/',
    secure: process.env.NODE_ENV === 'production',
    maxAge: ONE_YEAR,
  });
}

function baseHeaders(role: 'admin' | 'beta', betaId: string): Record<string, string> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'X-Mesh-Role': role,
  };
  const tok = internalToken();
  if (tok) headers['X-Mesh-Internal-Token'] = tok;
  if (role !== 'admin') headers['X-Mesh-Beta-Id'] = betaId;
  return headers;
}

export async function GET(req: NextRequest) {
  const role = await roleOf(req);
  const { id, isNew } = ensureBetaId(req);
  const apiRes = await fetch(`${internalApiBase()}/api/v1/ask/quota`, {
    headers: baseHeaders(role, id),
  });
  const res = new NextResponse(await apiRes.text(), {
    status: apiRes.status,
    headers: { 'Content-Type': 'application/json' },
  });
  if (isNew && role !== 'admin') setBetaCookie(res, id);
  return res;
}

export async function POST(req: NextRequest) {
  const role = await roleOf(req);
  const { id, isNew } = ensureBetaId(req);
  const field = new URL(req.url).searchParams.get('field');
  const target = `${internalApiBase()}/api/v1/ask${
    field ? `?field=${encodeURIComponent(field)}` : ''
  }`;
  const apiRes = await fetch(target, {
    method: 'POST',
    headers: baseHeaders(role, id),
    body: await req.text(),
  });
  const res = new NextResponse(await apiRes.text(), {
    status: apiRes.status,
    headers: { 'Content-Type': 'application/json' },
  });
  if (isNew && role !== 'admin') setBetaCookie(res, id);
  return res;
}

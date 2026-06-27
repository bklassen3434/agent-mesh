import { type NextRequest, NextResponse } from 'next/server';

import { ROLE_COOKIE, signRole } from '@/lib/auth';

const THIRTY_DAYS = 60 * 60 * 24 * 30;

export async function POST(req: NextRequest) {
  const expected = (process.env.MESH_ADMIN_PASSWORD ?? '').trim();
  if (!expected) {
    return NextResponse.json(
      { error: 'Admin login is not configured on this deployment.' },
      { status: 503 },
    );
  }
  const body = (await req.json().catch(() => ({}))) as { password?: unknown };
  if (typeof body.password !== 'string' || body.password !== expected) {
    return NextResponse.json({ error: 'Incorrect password.' }, { status: 401 });
  }
  const res = NextResponse.json({ role: 'admin' });
  res.cookies.set(ROLE_COOKIE, await signRole('admin'), {
    httpOnly: true,
    sameSite: 'lax',
    path: '/',
    secure: process.env.NODE_ENV === 'production',
    maxAge: THIRTY_DAYS,
  });
  return res;
}

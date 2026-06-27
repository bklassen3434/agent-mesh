// Exit admin entirely (lock). Clears the role + preview cookies, returning the
// browser to an ordinary beta visitor.
import { NextResponse } from 'next/server';

import { PREVIEW_COOKIE, ROLE_COOKIE } from '@/lib/auth';

export async function POST() {
  const res = NextResponse.json({ ok: true });
  res.cookies.set(ROLE_COOKIE, '', { httpOnly: true, path: '/', maxAge: 0 });
  res.cookies.set(PREVIEW_COOKIE, '', { path: '/', maxAge: 0 });
  return res;
}

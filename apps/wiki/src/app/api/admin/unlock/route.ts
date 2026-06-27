// Admin unlock by capability URL. The owner visits
//   /api/admin/unlock?token=<MESH_ADMIN_TOKEN>
// (bookmark it) to set the signed admin role cookie, then is redirected home.
// A wrong/absent token gives nothing away — it just lands on the public home as
// a beta visitor, with no error and no hint that admin exists.
import { type NextRequest, NextResponse } from 'next/server';

import { PREVIEW_COOKIE, ROLE_COOKIE, signRole, verifyAdminToken } from '@/lib/auth';

const THIRTY_DAYS = 60 * 60 * 24 * 30;

export async function GET(req: NextRequest) {
  const token = req.nextUrl.searchParams.get('token');
  // Relative Location so the browser resolves it against its own origin — the
  // standalone server reports its bind address (0.0.0.0) as the host, which a
  // browser can't follow.
  const res = new NextResponse(null, { status: 307, headers: { Location: '/' } });
  if (!verifyAdminToken(token)) return res;

  res.cookies.set(ROLE_COOKIE, await signRole('admin'), {
    httpOnly: true,
    sameSite: 'lax',
    path: '/',
    secure: process.env.NODE_ENV === 'production',
    maxAge: THIRTY_DAYS,
  });
  // Start in real admin mode, not a stale preview.
  res.cookies.set(PREVIEW_COOKIE, '', { path: '/', maxAge: 0 });
  return res;
}

import { type NextRequest, NextResponse } from 'next/server';

import { adminLoginConfigured, ROLE_COOKIE, resolveRole } from '@/lib/auth';

export async function GET(req: NextRequest) {
  const role = await resolveRole(req.cookies.get(ROLE_COOKIE)?.value);
  return NextResponse.json({ role, loginConfigured: adminLoginConfigured() });
}

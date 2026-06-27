// Server-only auth helpers (import `next/headers`). Use from server components,
// layouts, and route handlers — never from middleware (Edge can't read
// `next/headers`; it uses `request.cookies` instead).
import { cookies } from 'next/headers';

import { FIELD_COOKIE, ROLE_COOKIE, type Role, resolveRole } from '@/lib/auth';

export { adminLoginConfigured } from '@/lib/auth';

export const DEFAULT_FIELD = 'ai-robotics';

/** The viewer's role for the current request (admin in open mode). */
export async function getRole(): Promise<Role> {
  const store = await cookies();
  return resolveRole(store.get(ROLE_COOKIE)?.value);
}

/** The topic (field slug) the viewer has selected, falling back to the default. */
export async function getField(): Promise<string> {
  const store = await cookies();
  return store.get(FIELD_COOKIE)?.value || DEFAULT_FIELD;
}

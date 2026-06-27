// Server-only auth helpers (import `next/headers`). Use from server components,
// layouts, and route handlers — never from middleware (Edge can't read
// `next/headers`; it uses `request.cookies` instead).
import { cookies } from 'next/headers';

import {
  FIELD_COOKIE,
  PREVIEW_COOKIE,
  ROLE_COOKIE,
  type Role,
  type ViewState,
  resolveView,
} from '@/lib/auth';

export { adminAuthConfigured } from '@/lib/auth';

export const DEFAULT_FIELD = 'ai-robotics';

/** The full view for the current request (real role, effective role, preview). */
export async function getView(): Promise<ViewState> {
  const store = await cookies();
  return resolveView(store.get(ROLE_COOKIE)?.value, store.get(PREVIEW_COOKIE)?.value);
}

/** The effective (preview-aware) role — what the page should render as. */
export async function getRole(): Promise<Role> {
  return (await getView()).effectiveRole;
}

/** The topic (field slug) the viewer has selected, falling back to the default. */
export async function getField(): Promise<string> {
  const store = await cookies();
  return store.get(FIELD_COOKIE)?.value || DEFAULT_FIELD;
}

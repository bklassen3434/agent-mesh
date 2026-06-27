// Auth primitives shared by middleware (Edge), route handlers (Node), and
// server components. Pure: no `next/headers` import, so it is safe in middleware.
//
// Model: there is no login form and no password. The owner unlocks admin by
// visiting a secret capability URL carrying MESH_ADMIN_TOKEN, which sets a
// signed, httpOnly role cookie. A normal visitor has no way in and no hint that
// admin exists — they are an anonymous "beta". An admin can *preview* the beta
// experience (a cookie that only ever downgrades admin→beta, never the reverse),
// with a clear way back; a beta can never elevate.

export const ROLE_COOKIE = 'mesh_role';
export const BETA_COOKIE = 'mesh_beta_id';
export const FIELD_COOKIE = 'mesh_field';
export const PREVIEW_COOKIE = 'mesh_preview';

export type Role = 'admin' | 'beta';

export interface ViewState {
  /** What the viewer actually is (a held admin cookie, or open mode). */
  realRole: Role;
  /** What the UI should render — admin previewing-as-beta resolves to beta. */
  effectiveRole: Role;
  /** True when a real admin is currently previewing the beta experience. */
  isPreviewing: boolean;
}

function secret(): string {
  return process.env.AUTH_SECRET ?? 'dev-insecure-secret-change-me';
}

async function hmacHex(message: string): Promise<string> {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw',
    enc.encode(secret()),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const sig = await crypto.subtle.sign('HMAC', key, enc.encode(message));
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// The role cookie value = `<role>.<hmac(role)>`. The signature makes it
// unforgeable; httpOnly keeps it out of reach of page scripts.
export async function signRole(role: Role): Promise<string> {
  return `${role}.${await hmacHex(role)}`;
}

export async function verifyRoleCookie(value: string | undefined): Promise<Role | null> {
  if (!value) return null;
  const dot = value.lastIndexOf('.');
  if (dot < 0) return null;
  const role = value.slice(0, dot);
  const sig = value.slice(dot + 1);
  if (role !== 'admin' && role !== 'beta') return null;
  return safeEqual(sig, await hmacHex(role)) ? (role as Role) : null;
}

/** Whether the admin gate is armed. With no MESH_ADMIN_TOKEN set, the wiki runs
 * in "open mode" — everyone is admin (the local-dev / single-user posture). Set
 * the token to make every visitor a beta until they unlock with it. */
export function adminAuthConfigured(): boolean {
  return Boolean((process.env.MESH_ADMIN_TOKEN ?? '').trim());
}

/** Constant-time check of an unlock token against MESH_ADMIN_TOKEN. */
export function verifyAdminToken(token: string | null | undefined): boolean {
  const expected = (process.env.MESH_ADMIN_TOKEN ?? '').trim();
  if (!expected || typeof token !== 'string') return false;
  return safeEqual(token, expected);
}

/** The viewer's real role: admin in open mode, else the verified cookie. */
export async function resolveRole(roleCookie: string | undefined): Promise<Role> {
  if (!adminAuthConfigured()) return 'admin';
  return (await verifyRoleCookie(roleCookie)) === 'admin' ? 'admin' : 'beta';
}

/** The full view for a request: real role, the effective (preview-aware) role,
 * and whether a real admin is previewing. Preview only ever downgrades. */
export async function resolveView(
  roleCookie: string | undefined,
  previewCookie: string | undefined,
): Promise<ViewState> {
  const realRole = await resolveRole(roleCookie);
  const isPreviewing = realRole === 'admin' && previewCookie === 'beta';
  return {
    realRole,
    effectiveRole: isPreviewing ? 'beta' : realRole,
    isPreviewing,
  };
}

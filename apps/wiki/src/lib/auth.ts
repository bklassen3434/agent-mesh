// Auth primitives shared by middleware (Edge), route handlers (Node), and
// server components. Pure: no `next/headers` import, so it is safe to pull into
// middleware. The wiki is the auth boundary — an admin signs in with a shared
// password and gets a signed, httpOnly role cookie; everyone else is an
// anonymous, rate-limited "beta" visitor.

export const ROLE_COOKIE = 'mesh_role';
export const BETA_COOKIE = 'mesh_beta_id';
export const FIELD_COOKIE = 'mesh_field';

export type Role = 'admin' | 'beta';

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

// Cookie value = `<role>.<hmac(role)>`. The signature is what makes the cookie
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

/** Whether admin login is configured. When false, the role gate is OFF — the
 * wiki runs in "open mode" and everyone is an admin (the pre-auth behavior).
 * Safe in Edge + Node: reads a non-public server env var available at runtime. */
export function adminLoginConfigured(): boolean {
  return Boolean((process.env.MESH_ADMIN_PASSWORD ?? '').trim());
}

/** The single source of truth for a request's role: admin in open mode,
 * otherwise the verified cookie (defaulting to beta). */
export async function resolveRole(cookieValue: string | undefined): Promise<Role> {
  if (!adminLoginConfigured()) return 'admin';
  return (await verifyRoleCookie(cookieValue)) === 'admin' ? 'admin' : 'beta';
}

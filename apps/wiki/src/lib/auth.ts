// Auth primitives shared by middleware (Edge), route handlers (Node), and
// server components. Pure: no `next/headers` import, so it is safe in middleware.
//
// Model: admin is a property of the *running instance*, not of the client.
// `MESH_ADMIN_MODE` is a server-side env flag — nothing the browser sends can
// grant admin, so there is no token to guess or leak. Run the wiki with the flag
// on for a local/non-exposed admin instance; the public deployment leaves it off
// and is beta-only (anonymous, view-only, rate-limited chat). On an admin
// instance the owner can preview the beta experience (a cookie that ONLY ever
// downgrades admin→beta), with a clear way back.

export const BETA_COOKIE = 'mesh_beta_id';
export const FIELD_COOKIE = 'mesh_field';
export const PREVIEW_COOKIE = 'mesh_preview';

export type Role = 'admin' | 'beta';

export interface ViewState {
  /** What this instance grants: admin when MESH_ADMIN_MODE is on, else beta. */
  realRole: Role;
  /** What the UI should render — admin previewing-as-beta resolves to beta. */
  effectiveRole: Role;
  /** True when an admin instance is currently previewing the beta experience. */
  isPreviewing: boolean;
}

/** Whether this instance grants admin. A server-only env flag — never sent by
 * the client — so a public visitor can never reach admin (nothing to guess).
 * Off by default: an exposed deployment is beta-only unless you opt in. */
export function adminModeEnabled(): boolean {
  const v = (process.env.MESH_ADMIN_MODE ?? '').trim().toLowerCase();
  return v === '1' || v === 'true' || v === 'yes' || v === 'on';
}

/** The full view for a request: the instance's role and the effective
 * (preview-aware) role. Preview only ever downgrades admin→beta. */
export function resolveView(previewCookie: string | undefined): ViewState {
  const realRole: Role = adminModeEnabled() ? 'admin' : 'beta';
  const isPreviewing = realRole === 'admin' && previewCookie === 'beta';
  return {
    realRole,
    effectiveRole: isPreviewing ? 'beta' : realRole,
    isPreviewing,
  };
}

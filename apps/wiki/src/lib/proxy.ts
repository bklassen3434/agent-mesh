// Server-side helpers for the route handlers that proxy privileged calls to the
// read API. The browser never talks to the API for these — it hits the wiki's
// own origin (first-party cookies, one auth checkpoint), and the wiki forwards
// server-to-server with the shared internal token attached.

/** Base URL the wiki server uses to reach the API (internal docker hostname). */
export function internalApiBase(): string {
  return (
    process.env.INTERNAL_API_URL ??
    process.env.NEXT_PUBLIC_API_URL ??
    'http://localhost:8000'
  );
}

/** Shared token proving a call came from the wiki server; undefined = unset. */
export function internalToken(): string | undefined {
  const t = (process.env.MESH_INTERNAL_TOKEN ?? '').trim();
  return t || undefined;
}

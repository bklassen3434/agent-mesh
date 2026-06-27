import { defineConfig, devices } from '@playwright/test';

/**
 * E2E config for the wiki. Two web servers boot before any test:
 *   1. the mock API (tests/mock-server.ts) on MOCK_API_PORT
 *   2. the production wiki (`next build && next start`) on WIKI_PORT, pointed at
 *      the mock via NEXT_PUBLIC_API_URL (baked into the client bundle at build)
 *      and INTERNAL_API_URL (read per-request by server components).
 *
 * Ports are env-overridable; nothing hardcodes a localhost URL in the specs.
 */
const MOCK_API_PORT = process.env.MOCK_API_PORT ?? '8787';
const WIKI_PORT = process.env.WIKI_PORT ?? '3100';
export const MOCK_API_URL = `http://localhost:${MOCK_API_PORT}`;
export const BASE_URL = `http://localhost:${WIKI_PORT}`;

// Make the resolved ports visible to worker processes (which inherit this
// process's env) so specs can derive the mock URL without hardcoding a port.
process.env.MOCK_API_PORT = MOCK_API_PORT;
process.env.WIKI_PORT = WIKI_PORT;

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: BASE_URL,
    actionTimeout: 10_000,
    trace: 'on-first-retry',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: [
    {
      command: 'npx tsx tests/mock-server.ts',
      url: `${MOCK_API_URL}/healthz`,
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
      env: { MOCK_API_PORT },
    },
    {
      command: 'npm run build && npm run start',
      url: BASE_URL,
      reuseExistingServer: !process.env.CI,
      timeout: 180_000,
      env: {
        PORT: WIKI_PORT,
        NEXT_PUBLIC_API_URL: MOCK_API_URL,
        INTERNAL_API_URL: MOCK_API_URL,
        // Exercise the admin surface (knowledge/agents/pipelines pages) in E2E.
        MESH_ADMIN_MODE: 'true',
      },
    },
  ],
});

import { expect, test } from '@playwright/test';

import { MOCK_API_URL } from '../../playwright.config';
import { PipelinesPage } from './pages/PipelinesPage';

// These tests mutate shared mock state (the schedules table), so run them in
// order and reset the mock before each so retries and ordering stay clean.
test.describe.configure({ mode: 'serial' });

test.beforeEach(async ({ request }) => {
  await request.post(`${MOCK_API_URL}/__test__/reset`);
});

test.describe('pipelines page', () => {
  test('the controller schedule card renders with correct interval and enabled state', async ({
    page,
  }) => {
    const pipelines = new PipelinesPage(page);
    await pipelines.goto();

    await expect(pipelines.card('Controller')).toBeVisible();
    await expect(pipelines.toggle('Controller')).toBeChecked();
    await expect(pipelines.intervalSelect('Controller')).toContainText('Every 6 hours');
  });

  test('toggling a schedule PATCHes the job and reflects the new state', async ({ page }) => {
    const pipelines = new PipelinesPage(page);
    await pipelines.goto();
    await expect(pipelines.toggle('Controller')).toBeChecked();

    const patch = page.waitForRequest(
      (r) => r.method() === 'PATCH' && r.url().endsWith('/api/v1/schedules/controller'),
    );
    await pipelines.toggle('Controller').click();

    expect((await patch).postDataJSON()).toEqual({ enabled: false });
    await expect(pipelines.toggle('Controller')).not.toBeChecked();
  });

  test('changing the interval PATCHes the new interval', async ({ page }) => {
    const pipelines = new PipelinesPage(page);
    await pipelines.goto();

    const patch = page.waitForRequest(
      (r) => r.method() === 'PATCH' && r.url().endsWith('/api/v1/schedules/controller'),
    );
    await pipelines.setInterval('Controller', 'Every 12 hours');

    expect((await patch).postDataJSON()).toEqual({ interval_hours: 12 });
    await expect(pipelines.intervalSelect('Controller')).toContainText('Every 12 hours');
  });

  test('Run now on the controller triggers a run and shows no error', async ({ page }) => {
    const pipelines = new PipelinesPage(page);
    await pipelines.goto();

    const trigger = page.waitForRequest(
      (r) =>
        r.method() === 'POST' &&
        r.url().endsWith('/api/v1/pipelines/controller/trigger'),
    );
    await pipelines.runNow('Controller').click();
    await trigger;

    await expect(pipelines.errorBanner).toBeHidden();
    await expect(pipelines.runNow('Controller')).toBeEnabled();
  });

  test('Run now surfaces the "already in progress" error on a 409', async ({ page }) => {
    const pipelines = new PipelinesPage(page);
    await pipelines.goto();

    // Force the controller trigger to 409 for this test only.
    await page.route('**/api/v1/pipelines/controller/trigger', (route) =>
      route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'A run is already in progress' }),
      }),
    );
    await pipelines.runNow('Controller').click();

    await expect(pipelines.errorBanner).toBeVisible();
  });

  test('run history renders rows and a row expands to show detail', async ({ page }) => {
    const pipelines = new PipelinesPage(page);
    await pipelines.goto();

    expect(await pipelines.historyRows().count()).toBeGreaterThanOrEqual(1);

    await expect(pipelines.rowDetail).toBeHidden();
    await pipelines.expandFirstRow();
    await expect(pipelines.rowDetail).toBeVisible();
  });
});

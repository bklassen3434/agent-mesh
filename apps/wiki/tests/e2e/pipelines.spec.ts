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
  test('both schedule cards render with correct intervals and enabled state', async ({
    page,
  }) => {
    const pipelines = new PipelinesPage(page);
    await pipelines.goto();

    await expect(pipelines.card('Coordinator')).toBeVisible();
    await expect(pipelines.card('Skeptic sweep')).toBeVisible();

    await expect(pipelines.toggle('Coordinator')).toBeChecked();
    await expect(pipelines.toggle('Skeptic sweep')).toBeChecked();

    await expect(pipelines.intervalSelect('Coordinator')).toContainText('Every 6 hours');
    await expect(pipelines.intervalSelect('Skeptic sweep')).toContainText('Every day');
  });

  test('toggling a schedule PATCHes the job and reflects the new state', async ({ page }) => {
    const pipelines = new PipelinesPage(page);
    await pipelines.goto();
    await expect(pipelines.toggle('Coordinator')).toBeChecked();

    const patch = page.waitForRequest(
      (r) => r.method() === 'PATCH' && r.url().endsWith('/api/v1/schedules/pipeline'),
    );
    await pipelines.toggle('Coordinator').click();

    expect((await patch).postDataJSON()).toEqual({ enabled: false });
    await expect(pipelines.toggle('Coordinator')).not.toBeChecked();
  });

  test('changing the interval PATCHes the new interval', async ({ page }) => {
    const pipelines = new PipelinesPage(page);
    await pipelines.goto();

    const patch = page.waitForRequest(
      (r) => r.method() === 'PATCH' && r.url().endsWith('/api/v1/schedules/pipeline'),
    );
    await pipelines.setInterval('Coordinator', 'Every 12 hours');

    expect((await patch).postDataJSON()).toEqual({ interval_hours: 12 });
    await expect(pipelines.intervalSelect('Coordinator')).toContainText('Every 12 hours');
  });

  test('Run now on the coordinator triggers a run and shows no error', async ({ page }) => {
    const pipelines = new PipelinesPage(page);
    await pipelines.goto();

    const trigger = page.waitForRequest(
      (r) =>
        r.method() === 'POST' &&
        r.url().endsWith('/api/v1/pipelines/pipeline/trigger'),
    );
    await pipelines.runNow('Coordinator').click();
    await trigger;

    await expect(pipelines.errorBanner).toBeHidden();
    await expect(pipelines.runNow('Coordinator')).toBeEnabled();
  });

  test('Run now on the 409 job surfaces the "already in progress" error', async ({ page }) => {
    const pipelines = new PipelinesPage(page);
    await pipelines.goto();

    const trigger = page.waitForRequest(
      (r) =>
        r.method() === 'POST' &&
        r.url().endsWith('/api/v1/pipelines/skeptic_sweep/trigger'),
    );
    await pipelines.runNow('Skeptic sweep').click();
    await trigger;

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

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

    await expect(pipelines.card('Ingest')).toBeVisible();
    await expect(pipelines.card('Skeptic sweep')).toBeVisible();

    await expect(pipelines.toggle('Ingest')).toBeChecked();
    await expect(pipelines.toggle('Skeptic sweep')).toBeChecked();

    await expect(pipelines.intervalSelect('Ingest')).toContainText('Every 6 hours');
    await expect(pipelines.intervalSelect('Skeptic sweep')).toContainText('Every day');
  });

  test('toggling a schedule PATCHes the job and reflects the new state', async ({ page }) => {
    const pipelines = new PipelinesPage(page);
    await pipelines.goto();
    await expect(pipelines.toggle('Ingest')).toBeChecked();

    const patch = page.waitForRequest(
      (r) => r.method() === 'PATCH' && r.url().endsWith('/api/v1/schedules/ingest'),
    );
    await pipelines.toggle('Ingest').click();

    expect((await patch).postDataJSON()).toEqual({ enabled: false });
    await expect(pipelines.toggle('Ingest')).not.toBeChecked();
  });

  test('changing the interval PATCHes the new interval', async ({ page }) => {
    const pipelines = new PipelinesPage(page);
    await pipelines.goto();

    const patch = page.waitForRequest(
      (r) => r.method() === 'PATCH' && r.url().endsWith('/api/v1/schedules/ingest'),
    );
    await pipelines.setInterval('Ingest', 'Every 12 hours');

    expect((await patch).postDataJSON()).toEqual({ interval_hours: 12 });
    await expect(pipelines.intervalSelect('Ingest')).toContainText('Every 12 hours');
  });

  test('Run now on the coordinator triggers a run and shows no error', async ({ page }) => {
    const pipelines = new PipelinesPage(page);
    await pipelines.goto();

    const trigger = page.waitForRequest(
      (r) =>
        r.method() === 'POST' &&
        r.url().endsWith('/api/v1/pipelines/ingest/trigger'),
    );
    await pipelines.runNow('Ingest').click();
    await trigger;

    await expect(pipelines.errorBanner).toBeHidden();
    await expect(pipelines.runNow('Ingest')).toBeEnabled();
  });

  test('Run now on the 409 job surfaces the "already in progress" error', async ({ page }) => {
    const pipelines = new PipelinesPage(page);
    await pipelines.goto();

    const trigger = page.waitForRequest(
      (r) =>
        r.method() === 'POST' &&
        r.url().endsWith('/api/v1/pipelines/skeptic/trigger'),
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

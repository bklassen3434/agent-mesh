import { expect, test } from '@playwright/test';

test.describe('live mesh visualization', () => {
  test('renders the animated board with the knowledge-base core', async ({ page }) => {
    await page.goto('/live');
    await expect(page.getByRole('heading', { name: 'Live', level: 1 })).toBeVisible();

    const board = page.getByRole('img', { name: 'Live agent mesh activity' });
    await expect(board).toBeVisible();
    const box = await board.boundingBox();
    expect(box!.width).toBeGreaterThan(0);

    // the central knowledge base is labelled on the board
    await expect(board.getByText('KNOWLEDGE BASE')).toBeVisible();
    // the legend explains what a packet means
    await expect(page.getByText('Packet = one activation')).toBeVisible();
  });

  test('replays real activity into the feed', async ({ page }) => {
    await page.goto('/live');
    // the seeded firehose rows land in the activity feed, newest-first
    await expect(page.getByText('Activity feed')).toBeVisible();
    await expect(page.getByText('scout-source').first()).toBeVisible();
    await expect(page.getByText('synthesize-belief').first()).toBeVisible();
  });

  test('is field-scoped — an unknown field shows the waiting state', async ({ page }) => {
    await page.goto('/live?field=no-such-field');
    await expect(page.getByText('Waiting for the controller to act…')).toBeVisible();
  });
});

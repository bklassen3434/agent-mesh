import { expect, test } from '@playwright/test';

import { NavPage } from './pages/NavPage';

test.describe('top navigation', () => {
  test('all top-level nav links render', async ({ page }) => {
    const nav = new NavPage(page);
    await nav.goto('/knowledge/beliefs');

    await expect(nav.brand).toBeVisible();
    await expect(nav.dailyBrief).toBeVisible();
    await expect(nav.knowledgeTrigger).toBeVisible();
    await expect(nav.graph).toBeVisible();
    await expect(nav.connectors).toBeVisible();
  });

  test('Knowledge dropdown opens and shows all four sub-links', async ({ page }) => {
    const nav = new NavPage(page);
    await nav.goto('/knowledge/beliefs');
    await nav.openKnowledge();

    await expect(nav.knowledgeMenu.getByRole('menuitem')).toHaveCount(4);
    for (const label of ['Beliefs', 'Entities', 'Claims', 'Sources']) {
      await expect(nav.knowledgeItem(label)).toBeVisible();
    }
  });

  for (const { label, path } of [
    { label: 'Beliefs', path: '/knowledge/beliefs' },
    { label: 'Entities', path: '/knowledge/entities' },
    { label: 'Claims', path: '/knowledge/claims' },
    { label: 'Sources', path: '/knowledge/sources' },
  ]) {
    test(`Knowledge → ${label} navigates to ${path}`, async ({ page }) => {
      const nav = new NavPage(page);
      await nav.goto('/graph');
      await nav.openKnowledge();
      await nav.knowledgeItem(label).click();
      await expect(page).toHaveURL(new RegExp(`${path}$`));
    });
  }

  for (const { from, to } of [
    { from: '/beliefs', to: '/knowledge/beliefs' },
    { from: '/entities', to: '/knowledge/entities' },
    { from: '/claims', to: '/knowledge/claims' },
    { from: '/sources', to: '/knowledge/sources' },
  ]) {
    test(`old route ${from} redirects to ${to}`, async ({ page }) => {
      await page.goto(from);
      await expect(page).toHaveURL(new RegExp(`${to}$`));
    });
  }

  test('active link state reflects the current route', async ({ page }) => {
    const nav = new NavPage(page);

    await nav.goto('/graph');
    await nav.expectActive(nav.graph);
    await nav.expectInactive(nav.connectors);

    await nav.goto('/connectors');
    await nav.expectActive(nav.connectors);
    await nav.expectInactive(nav.graph);

    await nav.goto('/knowledge/beliefs');
    await nav.expectActive(nav.knowledgeTrigger);
    await nav.expectInactive(nav.graph);
  });
});

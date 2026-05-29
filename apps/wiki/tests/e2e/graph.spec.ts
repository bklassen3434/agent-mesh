import { expect, test } from '@playwright/test';

import { GraphPage } from './pages/GraphPage';

test.describe('knowledge graph', () => {
  test('renders a canvas with non-zero dimensions', async ({ page }) => {
    const graph = new GraphPage(page);
    await graph.goto();

    const box = await graph.canvas.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.width).toBeGreaterThan(0);
    expect(box!.height).toBeGreaterThan(0);
  });

  test('shows the cap notice when the node list is capped', async ({ page }) => {
    const graph = new GraphPage(page);
    await graph.goto();

    await expect(graph.capNotice).toBeVisible();
    await expect(graph.capNotice).toContainText('200');
  });

  test('controls panel exposes search, min belief count, and type filters', async ({
    page,
  }) => {
    const graph = new GraphPage(page);
    await graph.goto();

    await expect(graph.searchInput).toBeVisible();
    await expect(graph.minBeliefLabel).toBeVisible();
    await expect(graph.entityTypesLabel).toBeVisible();
    await expect(graph.resetButton).toBeVisible();
  });

  test('clicking an edge opens the relationship side panel', async ({ page }) => {
    const graph = new GraphPage(page);
    await graph.goto();

    await expect(graph.sidePanelTitle).toBeHidden();
    await graph.clickFirstEdge();

    await expect(graph.sidePanelTitle).toBeVisible();
    await expect(graph.sidePanelType).toBeVisible();
    await expect(graph.sidePanelClaims).toBeVisible();
    await expect(graph.sidePanelFromTo).toBeVisible();
  });
});

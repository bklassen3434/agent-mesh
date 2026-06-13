import { expect, test } from '@playwright/test';

import { AgentsPage } from './pages/AgentsPage';

test.describe('agents observability', () => {
  test('renders the agent graph canvas', async ({ page }) => {
    const agents = new AgentsPage(page);
    await agents.goto();
    await expect(agents.canvas).toBeVisible();
    const box = await agents.canvas.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.width).toBeGreaterThan(0);
  });

  test('clicking an agent reveals its memory and recent invocations', async ({ page }) => {
    const agents = new AgentsPage(page);
    await agents.goto();
    await agents.selectAgent('claim_extractor');
    // current memory: the agent's active heuristic text
    await expect(agents.detail).toContainText('self-reported');
    // recent invocations: the two seeded extract_claims calls
    await expect(agents.invocationRows()).toHaveCount(2);
  });

  test('drilling into an invocation shows its context + a Langfuse link', async ({ page }) => {
    const agents = new AgentsPage(page);
    await agents.goto();
    await agents.selectAgent('claim_extractor');
    await agents.invocationRows().first().click();
    const detail = agents.invocationDetail().first();
    await expect(detail).toBeVisible();
    // injected memory/context is surfaced
    await expect(detail).toContainText('Injected memory');
    await expect(detail).toContainText('self-reported');
    // input/output bounded captures are shown
    await expect(detail).toContainText('Input');
    await expect(detail).toContainText('Output');
    // deep link to the raw trace
    await expect(detail.getByRole('link', { name: /View trace in Langfuse/ })).toBeVisible();
  });

  test('is field-scoped — an unknown field shows the empty state', async ({ page }) => {
    const agents = new AgentsPage(page);
    await agents.goto('?field=no-such-field');
    await expect(
      page.getByText('No agent activity yet — run the pipeline to populate this view.'),
    ).toBeVisible();
  });
});

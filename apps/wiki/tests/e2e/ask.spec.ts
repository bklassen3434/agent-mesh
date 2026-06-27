import { expect, test } from '@playwright/test';

import { AskPage } from './pages/AskPage';

test.describe('ask page', () => {
  test('asking a question renders a grounded answer with a coverage badge', async ({
    page,
  }) => {
    const ask = new AskPage(page);
    await ask.goto();

    await ask.ask('Which system leads on locomotion?');
    await expect(ask.answer).toBeVisible();
    await expect(ask.coverageBadge).toContainText('Well supported');
    await expect(ask.answer).toContainText('leading system performs strongly');
  });

  test('citations open an in-place evidence popup without leaving the chat', async ({
    page,
  }) => {
    const ask = new AskPage(page);
    await ask.goto();
    await ask.ask('Which system leads on locomotion?');
    await expect(ask.answer).toBeVisible();

    // The inline [belief] citation is a button that opens the evidence popup —
    // the reader stays on /ask (no navigation to /knowledge).
    await ask.answer.getByRole('button', { name: '[belief]' }).click();
    await expect(ask.evidenceDialog).toBeVisible();
    await expect(ask.evidenceBody).toContainText('the field is converging');
    await expect(page).toHaveURL(/\/ask(\?|$)/);

    // Drill down: a supporting claim row swaps the popup to the claim detail.
    await ask.evidenceBody.getByText('achieves_score').first().click();
    await expect(ask.evidenceBody).toContainText('Excerpt 0');

    // Close, then a structured citation pill opens the same popup.
    await page.keyboard.press('Escape');
    await expect(ask.evidenceDialog).toBeHidden();
    await ask.answer.getByRole('button', { name: /^entity ent-0/ }).click();
    await expect(ask.evidenceDialog).toBeVisible();
    await expect(ask.evidenceBody).toContainText('Entity 0');
  });

  test('an out-of-corpus question shows the uncovered state', async ({ page }) => {
    const ask = new AskPage(page);
    await ask.goto();

    await ask.ask('Explain lattice quantum chromodynamics gauge theory.');
    await expect(ask.answer).toBeVisible();
    await expect(ask.coverageBadge).toContainText('Not covered');
    await expect(ask.answer).toContainText('no evidence');
  });

  test('the selected field is sent with the question', async ({ page }) => {
    const ask = new AskPage(page);
    await ask.goto('/ask?field=agribusiness');
    await expect(ask.fieldInput).toHaveValue('agribusiness');

    const resp = await ask.ask('What leads here?');
    expect(new URL(resp.url()).searchParams.get('field')).toBe('agribusiness');
    await expect(ask.answer).toContainText('In agribusiness');
  });
});

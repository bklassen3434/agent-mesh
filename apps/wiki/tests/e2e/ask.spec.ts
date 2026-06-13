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

  test('citation chips link to the detail pages', async ({ page }) => {
    const ask = new AskPage(page);
    await ask.goto();
    await ask.ask('Which system leads on locomotion?');
    await expect(ask.answer).toBeVisible();

    // The structured citation chips link to existing detail routes.
    const beliefLink = ask.answer.getByRole('link', { name: /belief/ }).first();
    await expect(beliefLink).toHaveAttribute('href', '/knowledge/beliefs/belief-0');

    // The inline [belief:…] citation in the prose is also a working link.
    const inline = ask.answer.getByRole('link', { name: '[belief]' });
    await expect(inline).toHaveAttribute('href', '/knowledge/beliefs/belief-0');
    await inline.click();
    await expect(page).toHaveURL(/\/knowledge\/beliefs\/belief-0$/);
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

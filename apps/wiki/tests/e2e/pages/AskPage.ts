import { type Locator, type Page, expect } from '@playwright/test';

/** The Ask page — a question box and the grounded answer card. */
export class AskPage {
  readonly page: Page;
  readonly heading: Locator;
  readonly questionInput: Locator;
  readonly fieldInput: Locator;
  readonly askButton: Locator;
  readonly answer: Locator;
  readonly coverageBadge: Locator;

  constructor(page: Page) {
    this.page = page;
    this.heading = page.getByRole('heading', { name: 'Ask', level: 1 });
    this.questionInput = page.getByLabel('Question');
    this.fieldInput = page.getByLabel('Field');
    this.askButton = page.getByRole('button', { name: 'Ask' });
    this.answer = page.getByTestId('ask-answer');
    this.coverageBadge = this.answer.locator('[aria-label^="coverage:"]');
  }

  async goto(path = '/ask') {
    await this.page.goto(path);
    await expect(this.heading).toBeVisible();
  }

  async ask(question: string) {
    await this.questionInput.fill(question);
    // The browser now talks to the wiki's own /api/ask proxy (the auth + quota
    // boundary), which forwards to the API server-side.
    const resp = this.page.waitForResponse(
      (r) => /\/api\/ask(\?|$)/.test(r.url()) && r.request().method() === 'POST',
    );
    await this.askButton.click();
    return resp;
  }
}

import { type Locator, type Page, expect } from '@playwright/test';

export class AgentsPage {
  readonly page: Page;
  readonly heading: Locator;
  readonly canvas: Locator;
  readonly detail: Locator;

  constructor(page: Page) {
    this.page = page;
    this.heading = page.getByRole('heading', { name: 'Agents', level: 1 });
    this.canvas = page.locator('canvas').first();
    this.detail = page.getByTestId('agent-detail');
  }

  async goto(query = '') {
    await this.page.goto(`/agents${query}`);
    await expect(this.heading).toBeVisible();
  }

  agentButton(agent: string): Locator {
    return this.page.getByRole('button', { name: agent, exact: true });
  }

  async selectAgent(agent: string) {
    await this.agentButton(agent).click();
    await expect(this.detail).toBeVisible();
  }

  invocationRows(): Locator {
    return this.page.getByTestId('invocation-row');
  }

  invocationDetail(): Locator {
    return this.page.getByTestId('invocation-detail');
  }
}

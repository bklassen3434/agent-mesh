import { type Locator, type Page, expect } from '@playwright/test';

/**
 * The /pipelines schedule-control panel. Each schedule card is anchored by its
 * uniquely-labelled enable switch ("<name> enabled"), which avoids colliding
 * with the run-history table where the same job names reappear as rows.
 */
export class PipelinesPage {
  readonly page: Page;
  readonly heading: Locator;
  readonly errorBanner: Locator;
  readonly runHistoryTable: Locator;

  constructor(page: Page) {
    this.page = page;
    this.heading = page.getByRole('heading', { name: 'Pipelines', level: 1 });
    this.errorBanner = page.getByText('A run is already in progress.');
    // The only <table> on the page is the run history; cards use a <dl>.
    this.runHistoryTable = page.locator('table');
  }

  async goto() {
    await this.page.goto('/pipelines');
    await expect(this.heading).toBeVisible();
  }

  card(name: string): Locator {
    return this.page
      .locator('div.rounded-lg.border')
      .filter({ has: this.page.getByRole('switch', { name: `${name} enabled` }) });
  }

  toggle(name: string): Locator {
    return this.card(name).getByRole('switch');
  }

  intervalSelect(name: string): Locator {
    return this.card(name).getByRole('combobox');
  }

  runNow(name: string): Locator {
    return this.card(name).getByRole('button', { name: 'Run now' });
  }

  async setInterval(name: string, optionLabel: string) {
    await this.intervalSelect(name).click();
    await this.page.getByRole('option', { name: optionLabel }).click();
  }

  /** Data rows in the run history (excludes the header and expansion rows). */
  historyRows(): Locator {
    return this.runHistoryTable.locator('tbody > tr');
  }

  async expandFirstRow() {
    await this.historyRows().first().click();
  }

  /** Detail block revealed when a run row is expanded. */
  get rowDetail(): Locator {
    return this.page.getByText('Agents / stages');
  }
}

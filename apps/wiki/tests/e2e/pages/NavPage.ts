import { type Locator, type Page, expect } from '@playwright/test';

/**
 * The top navigation bar. On a desktop viewport only the desktop links are in
 * the DOM — the mobile drawer and the Knowledge menu are Radix portals that
 * mount on open — so role/name locators are unambiguous without scoping.
 */
export class NavPage {
  readonly page: Page;
  readonly nav: Locator;
  readonly brand: Locator;
  readonly dailyBrief: Locator;
  readonly knowledgeTrigger: Locator;
  readonly graph: Locator;
  readonly connectors: Locator;

  constructor(page: Page) {
    this.page = page;
    this.nav = page.getByRole('navigation');
    this.brand = page.getByRole('link', { name: 'Agent Mesh', exact: true });
    this.dailyBrief = page.getByRole('link', { name: 'Daily Brief' });
    this.knowledgeTrigger = page.getByRole('button', { name: 'Knowledge' });
    this.graph = page.getByRole('link', { name: 'Graph', exact: true });
    this.connectors = page.getByRole('link', { name: 'Connectors', exact: true });
  }

  async goto(path = '/knowledge/beliefs') {
    await this.page.goto(path);
  }

  async openKnowledge() {
    await this.knowledgeTrigger.click();
    await expect(this.knowledgeMenu).toBeVisible();
  }

  get knowledgeMenu(): Locator {
    return this.page.getByRole('menu');
  }

  knowledgeItem(name: string): Locator {
    return this.knowledgeMenu.getByRole('menuitem', { name });
  }

  /** A nav element is "active" when it carries the font-medium weight class. */
  async expectActive(locator: Locator) {
    await expect(locator).toHaveClass(/font-medium/);
  }

  async expectInactive(locator: Locator) {
    await expect(locator).not.toHaveClass(/font-medium/);
  }
}

import { type Locator, type Page, expect } from '@playwright/test';

/**
 * The /graph cytoscape view. Edges are canvas-rendered, so a real pixel click
 * is unreliable; instead we reach the cytoscape Core via the container's
 * `_cyreg` handle and emit the same `tap` event the UI binds, which is what
 * opens the relationship side panel.
 */
export class GraphPage {
  readonly page: Page;
  readonly heading: Locator;
  readonly canvas: Locator;
  readonly capNotice: Locator;
  readonly searchInput: Locator;
  readonly minBeliefLabel: Locator;
  readonly entityTypesLabel: Locator;
  readonly resetButton: Locator;

  constructor(page: Page) {
    this.page = page;
    this.heading = page.getByRole('heading', { name: 'Knowledge graph', level: 1 });
    this.canvas = page.locator('canvas').first();
    this.capNotice = page.getByText(/Showing top \d+ of \d+ entities/);
    this.searchInput = page.getByPlaceholder('Search nodes…');
    this.minBeliefLabel = page.getByText('Min belief count');
    this.entityTypesLabel = page.getByText('Entity types', { exact: true });
    this.resetButton = page.getByRole('button', { name: 'Reset view' });
  }

  async goto() {
    await this.page.goto('/graph');
    await expect(this.heading).toBeVisible();
    await expect(this.canvas).toBeVisible();
  }

  /** Wait for cytoscape to mount and lay out at least one edge. */
  async waitForGraphReady() {
    await this.page.waitForFunction(() => {
      const c = document.querySelector('canvas');
      if (!c) return false;
      let el: unknown = c;
      while (el && !(el as { _cyreg?: unknown })._cyreg) {
        el = (el as Element).parentElement;
      }
      const cy = (el as { _cyreg?: { cy?: { edges: () => { length: number } } } })
        ?._cyreg?.cy;
      return !!cy && cy.edges().length > 0;
    });
  }

  /** Emit a tap on the first edge to open the relationship side panel. */
  async clickFirstEdge() {
    await this.waitForGraphReady();
    await this.page.evaluate(() => {
      let el: unknown = document.querySelector('canvas');
      while (el && !(el as { _cyreg?: unknown })._cyreg) {
        el = (el as Element).parentElement;
      }
      const cy = (el as { _cyreg: { cy: { edges: () => Array<{ emit: (e: string) => void }> } } })
        ._cyreg.cy;
      cy.edges()[0].emit('tap');
    });
  }

  // Relationship side-panel field labels (values are data-dependent; assert
  // the labels are present rather than exact values).
  get sidePanelTitle(): Locator {
    return this.page.getByText('Relationship', { exact: true });
  }
  get sidePanelType(): Locator {
    return this.page.getByText('Type', { exact: true });
  }
  get sidePanelClaims(): Locator {
    // "supporting claims" also appears in the page description prose, so match
    // the side-panel field label exactly.
    return this.page.getByText('Supporting claims', { exact: true });
  }
  get sidePanelFromTo(): Locator {
    return this.page.getByText('From → To', { exact: true });
  }
}

import { type Locator, type Page } from '@playwright/test';

export type KnowledgeSection = 'beliefs' | 'entities' | 'claims' | 'sources';

const HEADING: Record<KnowledgeSection, string> = {
  beliefs: 'Beliefs',
  entities: 'Entities',
  claims: 'Claims',
  sources: 'Sources',
};

/**
 * Generic page object for the /knowledge/* list routes. Each section renders
 * its items as links to detail pages (`/knowledge/<section>/<id>`), so a single
 * href-prefix selector counts items uniformly across cards and tables. The
 * pagination links target `/knowledge/<section>?…` (query, not a path segment)
 * and so never match.
 */
export class KnowledgePage {
  readonly page: Page;
  readonly section: KnowledgeSection;

  constructor(page: Page, section: KnowledgeSection) {
    this.page = page;
    this.section = section;
  }

  async goto() {
    await this.page.goto(`/knowledge/${this.section}`);
  }

  get heading(): Locator {
    return this.page.getByRole('heading', { name: HEADING[this.section], level: 1 });
  }

  /** Item links — one per belief card / entity row / claim row / source row. */
  items(): Locator {
    return this.page.locator(`a[href^="/knowledge/${this.section}/"]`);
  }
}

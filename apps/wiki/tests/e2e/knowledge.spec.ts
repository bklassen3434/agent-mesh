import { expect, test } from '@playwright/test';

import { KnowledgePage, type KnowledgeSection } from './pages/KnowledgePage';

const SECTIONS: { section: KnowledgeSection; heading: string; minItems: number }[] = [
  { section: 'beliefs', heading: 'Beliefs', minItems: 5 },
  { section: 'entities', heading: 'Entities', minItems: 5 },
  { section: 'claims', heading: 'Claims', minItems: 5 },
  { section: 'sources', heading: 'Sources', minItems: 3 },
];

test.describe('knowledge sections', () => {
  for (const { section, heading, minItems } of SECTIONS) {
    test(`/knowledge/${section} renders its heading and at least ${minItems} items`, async ({
      page,
    }) => {
      const knowledge = new KnowledgePage(page, section);
      await knowledge.goto();

      await expect(knowledge.heading).toBeVisible();
      await expect(knowledge.heading).toHaveText(heading);
      expect(await knowledge.items().count()).toBeGreaterThanOrEqual(minItems);
    });
  }
});

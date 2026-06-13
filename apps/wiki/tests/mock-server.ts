/**
 * Lightweight mock of the Agent Mesh read API (apps/api) for Playwright E2E.
 *
 * Returns fixture JSON for every endpoint the wiki calls. Response shapes are
 * hand-matched to the real FastAPI handlers and Pydantic models — field names
 * and types mirror apps/api/src/mesh_api and packages/mesh-models exactly so
 * the wiki's generated TypeScript contract holds against this server.
 *
 * Run via tsx; the port comes from MOCK_API_PORT (set by playwright.config.ts).
 */
import express, { type Request, type Response } from 'express';

const PORT = Number(process.env.MOCK_API_PORT ?? 8787);

// ── helpers ─────────────────────────────────────────────────────────────────

const iso = (d: Date) => d.toISOString();
const daysAgo = (n: number) => iso(new Date(Date.now() - n * 86_400_000));
const hoursFromNow = (n: number) => iso(new Date(Date.now() + n * 3_600_000));

function page<T>(items: T[]) {
  return { items, total: items.length, limit: 50, offset: 0 };
}

// ── fixtures ────────────────────────────────────────────────────────────────

const ENTITY_TYPES = ['model', 'paper', 'benchmark', 'method', 'person', 'lab'];

const entities = Array.from({ length: 6 }, (_, i) => ({
  id: `ent-${i}`,
  canonical_name: `Entity ${i}`,
  aliases: i % 2 === 0 ? [`alias-${i}a`, `alias-${i}b`] : [],
  type: ENTITY_TYPES[i % ENTITY_TYPES.length],
  attributes: {},
  created_at: daysAgo(30 - i),
  last_seen_at: daysAgo(i),
}));

const sources = Array.from({ length: 4 }, (_, i) => ({
  source: {
    id: `src-${i}`,
    type: ['arxiv', 'hn_post', 'github', 'blog'][i],
    url: `https://example.test/source/${i}`,
    author: i % 2 === 0 ? `Author ${i}` : null,
    published_at: daysAgo(20 - i),
    fetched_at: daysAgo(10 - i),
    raw_content_hash: `hash-${i}`,
    reliability_prior: 0.5 + i * 0.1,
  },
  claim_count: i + 2,
}));

const claims = Array.from({ length: 6 }, (_, i) => ({
  id: `claim-${i}`,
  predicate: `achieves_score`,
  subject_entity_id: `ent-${i % entities.length}`,
  object: { value: i, unit: 'pct' },
  source_id: `src-${i % sources.length}`,
  extracted_at: daysAgo(i),
  extracted_by_agent: 'claim_extractor',
  raw_excerpt: `Excerpt ${i}: the model reaches a new state of the art on the benchmark.`,
  status: 'active',
  confidence: 0.5 + (i % 5) * 0.08,
  superseded_by_claim_id: null,
  failure_mode: null,
}));

const beliefs = Array.from({ length: 6 }, (_, i) => ({
  id: `belief-${i}`,
  topic: `Topic ${i}`,
  statement: `Belief ${i}: the field is converging on a shared benchmark result.`,
  supporting_claim_ids: [`claim-${i % claims.length}`],
  contradicting_claim_ids: [],
  confidence: 0.5 + (i % 5) * 0.08,
  last_revised_at: daysAgo(i),
  revision_count: i,
  is_currently_held: true,
}));

const beliefSignals = beliefs.map((b, i) => ({
  belief_id: b.id,
  hype_substance_score: 0.3 + (i % 6) * 0.1,
  reproduction_count: i % 4,
}));

const pipelineRuns = [
  {
    id: 'run-pipeline-1',
    started_at: daysAgo(0),
    finished_at: iso(new Date(Date.now() - 0 * 86_400_000 + 90_000)),
    run_type: 'pipeline',
    triggered_by: 'scheduled',
    papers_scouted: 12,
    sources_inserted: 8,
    claims_inserted: 34,
    entities_created: 11,
    beliefs_created: 5,
    beliefs_revised: 3,
    avg_extraction_latency_ms: 820,
    errors: [],
  },
  {
    id: 'run-skeptic-1',
    started_at: daysAgo(1),
    finished_at: daysAgo(1),
    run_type: 'skeptic_sweep',
    triggered_by: 'manual',
    papers_scouted: 0,
    sources_inserted: 2,
    claims_inserted: 6,
    entities_created: 0,
    beliefs_created: 0,
    beliefs_revised: 4,
    avg_extraction_latency_ms: 0,
    errors: [],
  },
  {
    id: 'run-pipeline-2',
    started_at: daysAgo(2),
    finished_at: daysAgo(2),
    run_type: 'pipeline',
    triggered_by: 'scheduled',
    papers_scouted: 9,
    sources_inserted: 5,
    claims_inserted: 18,
    entities_created: 6,
    beliefs_created: 2,
    beliefs_revised: 1,
    avg_extraction_latency_ms: 640,
    errors: [
      {
        paper_id: 'arxiv:2401.00001',
        error_type: 'LLMResponseError',
        error_message: 'failed to parse structured output',
      },
    ],
  },
];

// Mutable so PATCH /schedules/:job_id is reflected by a follow-up GET.
// A /__test__/reset endpoint restores these defaults between mutation specs.
const defaultSchedules = () => [
  { job_id: 'pipeline', interval_hours: 6, enabled: true, updated_at: daysAgo(3) },
  { job_id: 'skeptic_sweep', interval_hours: 24, enabled: true, updated_at: daysAgo(3) },
];
let schedules = defaultSchedules();

const schedulerStatus = [
  { job_id: 'pipeline', next_run_at: hoursFromNow(4), last_run_at: daysAgo(0), state: 'idle' },
  { job_id: 'skeptic_sweep', next_run_at: hoursFromNow(20), last_run_at: daysAgo(1), state: 'idle' },
];

// Exactly NODE_CAP (200) nodes; total_entities above the cap so the wiki shows
// its "showing top N of M" notice. At least 10 edges between surviving nodes.
const GRAPH_NODE_COUNT = 200;
const GRAPH_TOTAL_ENTITIES = 217;
const GRAPH_TYPES = ['paper', 'model', 'benchmark', 'lab', 'person', 'concept'];

const graphNodes = Array.from({ length: GRAPH_NODE_COUNT }, (_, i) => ({
  id: `gnode-${i}`,
  label: `Node ${i}`,
  type: GRAPH_TYPES[i % GRAPH_TYPES.length],
  belief_count: 1 + (i % 8),
  last_claim_at: daysAgo(i % 30),
}));

const graphEdges = Array.from({ length: 15 }, (_, i) => ({
  source: `gnode-${i}`,
  target: `gnode-${i + 1}`,
  relationship_type: ['cites', 'benchmarks', 'authored_by', 'supersedes'][i % 4],
  claim_count: 1 + (i % 5),
}));

// ── app ─────────────────────────────────────────────────────────────────────

const app = express();
app.use(express.json());

// Permissive CORS so the wiki's browser-side PATCH/POST (preflighted) succeed.
app.use((req, res, next) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PATCH,OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Accept');
  if (req.method === 'OPTIONS') {
    res.sendStatus(204);
    return;
  }
  next();
});

app.get('/healthz', (_req, res) => {
  res.json({ status: 'ok', db_present: true });
});

// Test-only: restore mutable fixture state so mutation specs are isolated.
app.post('/__test__/reset', (_req, res) => {
  schedules = defaultSchedules();
  res.sendStatus(204);
});

app.get('/api/v1/stats', (_req, res) => {
  res.json({
    entities: entities.length,
    claims: claims.length,
    beliefs: beliefs.length,
    sources: sources.length,
    revisions: 4,
    pipeline_runs: pipelineRuns.length,
    last_pipeline_run_at: pipelineRuns[0].finished_at,
    last_pipeline_run_id: pipelineRuns[0].id,
  });
});

app.get('/api/v1/pipeline-runs', (req: Request, res: Response) => {
  const limit = Number(req.query.limit ?? 10);
  res.json(pipelineRuns.slice(0, limit));
});

app.get('/api/v1/entities', (_req, res) => res.json(page(entities)));
app.get('/api/v1/claims', (_req, res) => res.json(page(claims)));
app.get('/api/v1/beliefs', (_req, res) => res.json(page(beliefs)));
app.get('/api/v1/sources', (_req, res) => res.json(page(sources)));

// Registered before any /beliefs/:id route would be (mirrors the real API).
app.get('/api/v1/beliefs/signals', (_req, res) => res.json(beliefSignals));

app.get('/api/v1/graph/data', (_req, res) => {
  res.json({
    nodes: graphNodes,
    edges: graphEdges,
    total_entities: GRAPH_TOTAL_ENTITIES,
  });
});

app.get('/api/v1/schedules', (_req, res) => res.json(schedules));

app.patch('/api/v1/schedules/:jobId', (req: Request, res: Response) => {
  const sched = schedules.find((s) => s.job_id === req.params.jobId);
  if (!sched) {
    res.status(404).json({ detail: `Unknown job ${req.params.jobId}` });
    return;
  }
  const { interval_hours, enabled } = req.body ?? {};
  if (typeof interval_hours === 'number') sched.interval_hours = interval_hours;
  if (typeof enabled === 'boolean') sched.enabled = enabled;
  sched.updated_at = iso(new Date());
  res.json(sched);
});

app.get('/api/v1/scheduler/status', (_req, res) => res.json(schedulerStatus));

app.post('/api/v1/pipelines/:jobId/trigger', (req: Request, res: Response) => {
  // skeptic_sweep is wired to 409 ("already in progress"); pipeline returns 200.
  if (req.params.jobId === 'skeptic_sweep') {
    res.status(409).json({ detail: 'A run is already in progress' });
    return;
  }
  if (req.params.jobId !== 'pipeline') {
    res.status(404).json({ detail: `Unknown job ${req.params.jobId}` });
    return;
  }
  res.json({ run_id: `triggered-${Date.now()}`, triggered_at: iso(new Date()) });
});

app.get('/api/v1/briefing', (_req, res) => {
  res.json({ date: daysAgo(0).slice(0, 10), profile_excerpt: '', sections: [] });
});

app.post('/api/v1/ask', (req: Request, res: Response) => {
  const question = String(req.body?.question ?? '');
  const field = String(req.query.field ?? 'ai-robotics');
  // An explicitly "nothing-known" question exercises the uncovered state.
  if (/nothing|unknown|chromodynamics/i.test(question)) {
    res.json({
      answer_markdown: 'The mesh has no evidence on this question.',
      citations: [],
      coverage: 'uncovered',
      caveats: [],
    });
    return;
  }
  res.json({
    answer_markdown:
      `In ${field}, the leading system performs strongly [belief:belief-0]. ` +
      `This is corroborated by a claim [claim:claim-0] about [entity:ent-0].`,
    citations: [
      { kind: 'belief', id: 'belief-0', quote: 'performs strongly' },
      { kind: 'claim', id: 'claim-0', quote: 'corroborated' },
      { kind: 'entity', id: 'ent-0', quote: 'leading system' },
    ],
    coverage: 'well_supported',
    caveats: ['Evidence is recent and may shift as new sources arrive.'],
  });
});

app.listen(PORT, () => {
  // eslint-disable-next-line no-console
  console.log(`[mock-api] listening on http://localhost:${PORT}`);
});

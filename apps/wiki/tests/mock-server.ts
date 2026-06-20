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
    run_type: 'controller',
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
    run_type: 'controller',
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
    run_type: 'controller',
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
  { job_id: 'controller', interval_hours: 6, enabled: true, updated_at: daysAgo(3) },
];
let schedules = defaultSchedules();

const schedulerStatus = [
  { job_id: 'controller', next_run_at: hoursFromNow(4), last_run_at: daysAgo(0), state: 'idle' },
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

// Agent observability (Phase 23) ----------------------------------------------

const agentRoster = [
  {
    agent: 'claim_extractor',
    invocations: 12,
    errors: 2,
    error_rate: 2 / 12,
    avg_latency_ms: 140.5,
    total_input_tokens: 4200,
    total_output_tokens: 900,
    total_cost_usd: 0.0123,
    last_active: daysAgo(0),
    last_run_id: 'run-pipeline-1',
  },
  {
    agent: 'sota_tracker',
    invocations: 4,
    errors: 0,
    error_rate: 0,
    avg_latency_ms: 60,
    total_input_tokens: 800,
    total_output_tokens: 200,
    total_cost_usd: 0.002,
    last_active: daysAgo(0),
    last_run_id: 'run-pipeline-1',
  },
  {
    agent: 'arxiv_scout',
    invocations: 3,
    errors: 1,
    error_rate: 1 / 3,
    avg_latency_ms: 220,
    total_input_tokens: 0,
    total_output_tokens: 0,
    total_cost_usd: 0,
    last_active: daysAgo(0),
    last_run_id: 'run-pipeline-1',
  },
];

const agentGraph = {
  nodes: [
    { id: 'coordinator', label: 'coordinator', role: 'coordinator', invocation_count: 19, error_rate: 0 },
    ...agentRoster.map((r) => ({
      id: r.agent,
      label: r.agent,
      role: 'agent',
      invocation_count: r.invocations,
      error_rate: r.error_rate,
    })),
  ],
  edges: agentRoster.map((r) => ({
    source: 'coordinator',
    target: r.agent,
    call_count: r.invocations,
    error_count: r.errors,
  })),
};

const heuristic = {
  id: 'heur-1',
  agent: 'claim_extractor',
  skill: 'extract_claims',
  source: 'hn_post',
  entity_id: null,
  heuristic: 'forum scores are self-reported — discount them',
  confidence: 0.82,
  provenance_run_ids: ['run-pipeline-1'],
  provenance_claim_ids: [],
  created_at: daysAgo(10),
  last_revised_at: daysAgo(2),
  revision_count: 1,
  expires_at: hoursFromNow(720),
  is_currently_active: true,
};

const claimExtractorInvocations = [
  {
    id: 'inv-1',
    run_id: 'run-pipeline-1',
    field_id: 'ai-robotics',
    agent: 'claim_extractor',
    skill: 'extract_claims',
    traceparent: '00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01',
    trace_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    status: 'ok',
    error_type: null,
    error_message: null,
    input_summary: { truncated: false, preview: '{"paper": {"title": "A study"}}', keys: ['field_id', 'paper'] },
    output_summary: { truncated: false, preview: '{"claims": [{"predicate": "achieves_score"}]}', keys: ['claims', 'model', 'usage'] },
    memory_block: '=== LEARNED HEURISTICS ===\n- (confidence 0.82) forum scores are self-reported',
    applied_heuristic_ids: ['heur-1'],
    system_prefix_hash: 'deadbeefcafef00d',
    model: 'claude-haiku-4-5',
    latency_ms: 138,
    input_tokens: 350,
    output_tokens: 80,
    cost_usd: 0.0009,
    created_at: daysAgo(0),
  },
  {
    id: 'inv-2',
    run_id: 'run-pipeline-1',
    field_id: 'ai-robotics',
    agent: 'claim_extractor',
    skill: 'extract_claims',
    traceparent: null,
    trace_id: null,
    status: 'error',
    error_type: 'SkillCallError',
    error_message: 'provider timed out',
    input_summary: { truncated: false, preview: '{"paper": {"title": "Another"}}', keys: ['field_id', 'paper'] },
    output_summary: null,
    memory_block: null,
    applied_heuristic_ids: [] as string[],
    system_prefix_hash: null,
    model: null,
    latency_ms: 80,
    input_tokens: null,
    output_tokens: null,
    cost_usd: null,
    created_at: daysAgo(0),
  },
];

const invocationsByAgent: Record<string, typeof claimExtractorInvocations> = {
  claim_extractor: claimExtractorInvocations,
  sota_tracker: [],
  arxiv_scout: [],
};

const isDefaultField = (req: Request) => {
  const f = req.query.field;
  return f === undefined || f === 'ai-robotics';
};

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
  // The controller is the only job; it returns 200. The "already in progress"
  // (409) path is exercised by a per-test route override in the e2e spec.
  if (req.params.jobId !== 'controller') {
    res.status(404).json({ detail: `Unknown job ${req.params.jobId}` });
    return;
  }
  res.json({ run_id: `triggered-${Date.now()}`, triggered_at: iso(new Date()) });
});

app.get('/api/v1/briefing', (_req, res) => {
  res.json({ date: daysAgo(0).slice(0, 10), profile_excerpt: '', sections: [] });
});

// Agent observability (Phase 23). Field-scoped: a non-default field is empty.
app.get('/api/v1/agents', (req: Request, res: Response) => {
  res.json(isDefaultField(req) ? agentRoster : []);
});

app.get('/api/v1/agents/graph', (req: Request, res: Response) => {
  res.json(
    isDefaultField(req)
      ? agentGraph
      : { nodes: [{ id: 'coordinator', label: 'coordinator', role: 'coordinator', invocation_count: 0, error_rate: 0 }], edges: [] },
  );
});

// Static path registered before the dynamic /:agent/* routes.
app.get('/api/v1/agents/invocations/:id', (req: Request, res: Response) => {
  const inv = claimExtractorInvocations.find((i) => i.id === req.params.id);
  if (!inv) {
    res.status(404).json({ detail: 'Invocation not found' });
    return;
  }
  res.json({
    invocation: inv,
    applied_heuristics: inv.applied_heuristic_ids.includes('heur-1')
      ? [{ id: heuristic.id, heuristic: heuristic.heuristic, confidence: heuristic.confidence }]
      : [],
    langfuse_url: inv.trace_id ? `https://langfuse.test/trace/${inv.trace_id}` : null,
  });
});

app.get('/api/v1/agents/:agent/invocations', (req: Request, res: Response) => {
  if (!isDefaultField(req)) {
    res.json([]);
    return;
  }
  res.json(invocationsByAgent[req.params.agent] ?? []);
});

app.get('/api/v1/agents/:agent/memory', (req: Request, res: Response) => {
  const heuristics = req.params.agent === 'claim_extractor' && isDefaultField(req) ? [heuristic] : [];
  res.json({
    agent: req.params.agent,
    heuristics,
    episodic:
      req.params.agent === 'claim_extractor' && isDefaultField(req)
        ? [{ agent: 'claim_extractor', skill: 'extract_claims', event_type: 'extraction', action_summary: 'extracted 3 claims from a paper', outcome: { label: 'confirmed' } }]
        : [],
  });
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

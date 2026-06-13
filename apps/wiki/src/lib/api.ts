import type { components, paths } from './api-types';

type Schemas = components['schemas'];

export type Entity = Schemas['Entity'];
export type Claim = Schemas['Claim'];
export type Belief = Schemas['Belief'];
export type Source = Schemas['Source'];
export type BeliefRevision = Schemas['BeliefRevision'];
export type Relationship = Schemas['Relationship'];
export type PipelineRun = Schemas['PipelineRun'];
export type Stats = Schemas['StatsResponse'];
export type Health = Schemas['HealthResponse'];
export type EntityDetail = Schemas['EntityDetail'];
export type ClaimDetail = Schemas['ClaimDetail'];
export type SourceDetail = Schemas['SourceDetail'];
export type SourceWithCount = Schemas['SourceWithCount'];
export type BeliefDetail = Schemas['BeliefDetail'];
export type BeliefSignals = Schemas['BeliefSignals'];
export type ClaimWithContext = Schemas['ClaimWithContext'];
export type GraphResponse = Schemas['GraphResponse'];
export type GraphNode = Schemas['GraphNode'];
export type GraphEdge = Schemas['GraphEdge'];
export type GraphData = Schemas['GraphData'];
export type GraphDataNode = Schemas['GraphDataNode'];
export type GraphDataEdge = Schemas['GraphDataEdge'];
export type Schedule = Schemas['Schedule'];
export type ScheduleUpdate = Schemas['ScheduleUpdate'];
export type TriggerResult = Schemas['TriggerResult'];
export type SchedulerJobStatus = Schemas['SchedulerJobStatus'];
export type BeliefSignalSummary = Schemas['BeliefSignalSummary'];
export type RevisionWithTriggers = Schemas['RevisionWithTriggers'];
export type SkepticActivityItem = Schemas['SkepticActivityItem'];
export type Briefing = Schemas['Briefing'];
export type BriefingSection = Schemas['BriefingSection'];
export type PersonalizedItem = Schemas['PersonalizedItem'];

// Agent observability (Phase 23)
export type AgentRosterEntry = Schemas['AgentRosterEntry'];
export type AgentInvocation = Schemas['AgentInvocation'];
export type AgentInvocationDetail = Schemas['AgentInvocationDetail'];
export type AgentMemory = Schemas['AgentMemory'];
export type AgentHeuristic = Schemas['AgentHeuristic'];
export type AgentGraph = Schemas['AgentGraph'];
export type AgentGraphNode = Schemas['AgentGraphNode'];
export type AgentGraphEdge = Schemas['AgentGraphEdge'];
export type ResolvedHeuristic = Schemas['ResolvedHeuristic'];

export type PageEntity = Schemas['Page_Entity_'];
export type PageClaim = Schemas['Page_Claim_'];
export type PageBelief = Schemas['Page_Belief_'];
export type PageSource = Schemas['Page_SourceWithCount_'];

export type Paths = paths;

function baseUrl(): string {
  // On the server (RSC), prefer the internal docker hostname.
  // In the browser, the public URL is the only one reachable.
  if (typeof window === 'undefined') {
    return process.env.INTERNAL_API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';
  }
  return process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';
}

export interface ApiOptions {
  query?: Record<string, string | number | boolean | undefined | null>;
  next?: { revalidate?: number | false; tags?: string[] };
  signal?: AbortSignal;
}

function buildUrl(path: string, query?: ApiOptions['query']): string {
  const url = new URL(path.startsWith('http') ? path : `${baseUrl()}${path}`);
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== '') {
        url.searchParams.set(k, String(v));
      }
    }
  }
  return url.toString();
}

export async function apiGet<T>(path: string, opts: ApiOptions = {}): Promise<T> {
  const url = buildUrl(path, opts.query);
  const res = await fetch(url, {
    method: 'GET',
    headers: { Accept: 'application/json' },
    // App Router fetch caching: opt in to revalidation per call.
    next: opts.next ?? { revalidate: 0 },
    signal: opts.signal,
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new ApiError(res.status, `GET ${path} → ${res.status} ${res.statusText} ${text}`);
  }
  return (await res.json()) as T;
}

export async function apiSend<T>(
  method: 'POST' | 'PATCH',
  path: string,
  body?: unknown,
): Promise<T> {
  const url = buildUrl(path);
  const res = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new ApiError(res.status, `${method} ${path} → ${res.status} ${res.statusText} ${text}`);
  }
  return (await res.json()) as T;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = 'ApiError';
  }
}

// Typed convenience wrappers ------------------------------------------------

export const api = {
  stats: () => apiGet<Stats>('/api/v1/stats'),
  pipelineRuns: (limit = 10) => apiGet<PipelineRun[]>('/api/v1/pipeline-runs', { query: { limit } }),

  listEntities: (q: { type?: string; q?: string; limit?: number; offset?: number } = {}) =>
    apiGet<PageEntity>('/api/v1/entities', { query: q }),
  entity: (id: string) => apiGet<EntityDetail>(`/api/v1/entities/${encodeURIComponent(id)}`),

  listClaims: (
    q: { predicate?: string; source_id?: string; entity_id?: string; status?: string; limit?: number; offset?: number } = {},
  ) => apiGet<PageClaim>('/api/v1/claims', { query: q }),
  claim: (id: string) => apiGet<ClaimDetail>(`/api/v1/claims/${encodeURIComponent(id)}`),

  listBeliefs: (q: { topic?: string; currently_held?: boolean; limit?: number; offset?: number } = {}) =>
    apiGet<PageBelief>('/api/v1/beliefs', { query: q }),
  belief: (id: string) => apiGet<BeliefDetail>(`/api/v1/beliefs/${encodeURIComponent(id)}`),

  listSources: (q: { type?: string; limit?: number; offset?: number } = {}) =>
    apiGet<PageSource>('/api/v1/sources', { query: q }),
  source: (id: string) => apiGet<SourceDetail>(`/api/v1/sources/${encodeURIComponent(id)}`),

  beliefRevisions: (id: string, limit = 100) =>
    apiGet<RevisionWithTriggers[]>(
      `/api/v1/beliefs/${encodeURIComponent(id)}/revisions`,
      { query: { limit } },
    ),
  skepticRecent: (limit = 20) =>
    apiGet<SkepticActivityItem[]>('/api/v1/skeptic/recent', { query: { limit } }),

  briefing: (date?: string) =>
    apiGet<Briefing>('/api/v1/briefing', { query: date ? { date } : undefined }),

  graph: (q: { max_nodes?: number; max_edges?: number } = {}) =>
    apiGet<GraphResponse>('/api/v1/graph', { query: q }),
  graphData: () => apiGet<GraphData>('/api/v1/graph/data'),

  beliefSignals: (ids: string[] = []) => {
    const qs = ids.map((id) => `ids=${encodeURIComponent(id)}`).join('&');
    return apiGet<BeliefSignalSummary[]>(`/api/v1/beliefs/signals${qs ? `?${qs}` : ''}`);
  },

  // Agents page (Phase 23) --------------------------------------------------
  agentRoster: (field?: string) =>
    apiGet<AgentRosterEntry[]>('/api/v1/agents', { query: { field } }),
  agentGraph: (field?: string) =>
    apiGet<AgentGraph>('/api/v1/agents/graph', { query: { field } }),
  agentInvocations: (agent: string, field?: string, limit = 50) =>
    apiGet<AgentInvocation[]>(
      `/api/v1/agents/${encodeURIComponent(agent)}/invocations`,
      { query: { field, limit } },
    ),
  agentInvocation: (id: string) =>
    apiGet<AgentInvocationDetail>(`/api/v1/agents/invocations/${encodeURIComponent(id)}`),
  agentMemory: (agent: string, field?: string) =>
    apiGet<AgentMemory>(`/api/v1/agents/${encodeURIComponent(agent)}/memory`, {
      query: { field },
    }),

  // Pipelines page (Phase 9) ------------------------------------------------
  schedules: () => apiGet<Schedule[]>('/api/v1/schedules'),
  schedulerStatus: () => apiGet<SchedulerJobStatus[]>('/api/v1/scheduler/status'),
  updateSchedule: (jobId: string, body: ScheduleUpdate) =>
    apiSend<Schedule>('PATCH', `/api/v1/schedules/${encodeURIComponent(jobId)}`, body),
  triggerPipeline: (jobId: string) =>
    apiSend<TriggerResult>('POST', `/api/v1/pipelines/${encodeURIComponent(jobId)}/trigger`),
};

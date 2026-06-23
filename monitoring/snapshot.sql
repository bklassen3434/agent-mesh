-- monitoring/snapshot.sql
-- Emits ONE compact JSON line capturing a point-in-time health snapshot of the
-- live Agent Mesh knowledge store + controller activity. Run with:
--
--   psql -tAX -f snapshot.sql   (tuples-only, unaligned, no .psqlrc → single JSON line)
--
-- Designed to be appended to a JSONL file on a timer (see install-pi.sh). Every
-- field maps to something we can tune: growth → is scouting/synthesis working;
-- controller.* → STEP_CAP / ESCALATE_AFTER / cooldowns; cost.* → routing /
-- max_papers / model pins; quality.* → confidence weights / merge bands /
-- adjudication thresholds; errors → which skill is failing.
--
-- Fully-qualified table names (Phase 24 four-way schema split): knowledge.* for
-- the domain store, agents.* for observability, runtime.* for the LLM ledger.

SELECT json_build_object(
  'ts', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),

  -- ── knowledge base size + composition ──────────────────────────────────────
  'kb', json_build_object(
    'fields',         (SELECT count(*) FROM catalog.fields),
    'entities',       (SELECT count(*) FROM knowledge.entities),
    'sources',        (SELECT count(*) FROM knowledge.sources),
    'claims_total',   (SELECT count(*) FROM knowledge.claims),
    'claims_by_status', COALESCE((SELECT json_object_agg(status, c)
        FROM (SELECT status, count(*) c FROM knowledge.claims GROUP BY status) t), '{}'::json),
    'claims_by_type', COALESCE((SELECT json_object_agg(claim_type, c)
        FROM (SELECT claim_type, count(*) c FROM knowledge.claims GROUP BY claim_type) t), '{}'::json),
    'beliefs_total',  (SELECT count(*) FROM knowledge.beliefs),
    'beliefs_held',   (SELECT count(*) FROM knowledge.beliefs WHERE is_currently_held),
    'belief_revisions', (SELECT count(*) FROM knowledge.belief_revisions),
    'relationships',  (SELECT count(*) FROM knowledge.relationships),
    'investigations_total', (SELECT count(*) FROM knowledge.investigations),
    'investigations_by_status', COALESCE((SELECT json_object_agg(status, c)
        FROM (SELECT status, count(*) c FROM knowledge.investigations GROUP BY status) t), '{}'::json),
    'investigations_by_origin', COALESCE((SELECT json_object_agg(origin, c)
        FROM (SELECT origin, count(*) c FROM knowledge.investigations GROUP BY origin) t), '{}'::json)
  ),

  -- ── quality of the belief set (held beliefs only) ──────────────────────────
  'quality', json_build_object(
    'belief_conf_avg', (SELECT round(avg(confidence)::numeric, 3) FROM knowledge.beliefs WHERE is_currently_held),
    'belief_conf_min', (SELECT round(min(confidence)::numeric, 3) FROM knowledge.beliefs WHERE is_currently_held),
    'belief_conf_max', (SELECT round(max(confidence)::numeric, 3) FROM knowledge.beliefs WHERE is_currently_held),
    'belief_conf_bands', (SELECT json_build_object(
        'lt_0_3',    count(*) FILTER (WHERE confidence < 0.3),
        'b_0_3_0_6', count(*) FILTER (WHERE confidence >= 0.3 AND confidence < 0.6),
        'b_0_6_0_8', count(*) FILTER (WHERE confidence >= 0.6 AND confidence < 0.8),
        'ge_0_8',    count(*) FILTER (WHERE confidence >= 0.8)
      ) FROM knowledge.beliefs WHERE is_currently_held),
    'avg_support_per_held_belief', (SELECT round(avg(cardinality(supporting_claim_ids))::numeric, 2)
        FROM knowledge.beliefs WHERE is_currently_held),
    'contradicting_links_held', (SELECT COALESCE(sum(cardinality(contradicting_claim_ids)), 0)
        FROM knowledge.beliefs WHERE is_currently_held),
    'skeptic_counter_claims', (SELECT count(*) FROM knowledge.claims WHERE failure_mode IS NOT NULL)
  ),

  -- ── controller / agent activity (the loop's pulse) ─────────────────────────
  'controller', json_build_object(
    'invocations_total', (SELECT count(*) FROM agents.agent_invocations),
    'invocations_24h',   (SELECT count(*) FROM agents.agent_invocations WHERE created_at > now() - interval '24 hours'),
    'invocations_1h',    (SELECT count(*) FROM agents.agent_invocations WHERE created_at > now() - interval '1 hour'),
    'distinct_runs_24h', (SELECT count(DISTINCT run_id) FROM agents.agent_invocations WHERE created_at > now() - interval '24 hours'),
    'last_invocation_at',(SELECT to_char(max(created_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') FROM agents.agent_invocations),
    'by_skill_24h', COALESCE((SELECT json_object_agg(skill, c)
        FROM (SELECT skill, count(*) c FROM agents.agent_invocations
              WHERE created_at > now() - interval '24 hours' GROUP BY skill) t), '{}'::json),
    'by_status_24h', COALESCE((SELECT json_object_agg(status, c)
        FROM (SELECT status, count(*) c FROM agents.agent_invocations
              WHERE created_at > now() - interval '24 hours' GROUP BY status) t), '{}'::json),
    'errors_24h', (SELECT count(*) FROM agents.agent_invocations
        WHERE status = 'error' AND created_at > now() - interval '24 hours'),
    'errors_by_type_24h', COALESCE((SELECT json_object_agg(error_type, c)
        FROM (SELECT COALESCE(error_type, 'unknown') error_type, count(*) c FROM agents.agent_invocations
              WHERE status = 'error' AND created_at > now() - interval '24 hours' GROUP BY 1) t), '{}'::json)
  ),

  -- ── LLM spend (efficiency + routing-tier split) ────────────────────────────
  'cost', json_build_object(
    'cost_usd_total',  (SELECT round(COALESCE(sum(estimated_cost_usd), 0)::numeric, 4) FROM runtime.llm_usage),
    'cost_usd_24h',    (SELECT round(COALESCE(sum(estimated_cost_usd), 0)::numeric, 4) FROM runtime.llm_usage WHERE created_at > now() - interval '24 hours'),
    'tokens_in_24h',   (SELECT COALESCE(sum(input_tokens), 0) FROM runtime.llm_usage WHERE created_at > now() - interval '24 hours'),
    'tokens_out_24h',  (SELECT COALESCE(sum(output_tokens), 0) FROM runtime.llm_usage WHERE created_at > now() - interval '24 hours'),
    'by_model_24h', COALESCE((SELECT json_object_agg(model, cost)
        FROM (SELECT COALESCE(model, 'unknown') model, round(sum(estimated_cost_usd)::numeric, 4) cost
              FROM runtime.llm_usage WHERE created_at > now() - interval '24 hours' GROUP BY 1) t), '{}'::json),
    'by_skill_24h', COALESCE((SELECT json_object_agg(skill_id, cost)
        FROM (SELECT COALESCE(skill_id, 'unknown') skill_id, round(sum(estimated_cost_usd)::numeric, 4) cost
              FROM runtime.llm_usage WHERE created_at > now() - interval '24 hours' GROUP BY 1) t), '{}'::json)
  ),

  -- ── liveness watermarks (is anything actually moving?) ─────────────────────
  'liveness', json_build_object(
    'last_claim_extracted_at',  (SELECT to_char(max(extracted_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') FROM knowledge.claims),
    'last_source_fetched_at',   (SELECT to_char(max(fetched_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') FROM knowledge.sources),
    'last_belief_revision_at',  (SELECT to_char(max(revised_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') FROM knowledge.belief_revisions)
  )
);

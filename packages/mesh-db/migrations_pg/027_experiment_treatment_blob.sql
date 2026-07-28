-- Generalize a shadow experiment's candidate from a prompt string to an opaque
-- config blob, so any actuator kind can be tested: a prompt ({"prompt": "..."}),
-- confidence weights ({"support_weight": .., "attack_weight": ..}), decay
-- thresholds, etc. The actuator for the experiment's component is the only thing
-- that interprets it.
ALTER TABLE agents.improvement_experiments
    ADD COLUMN IF NOT EXISTS treatment JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE agents.improvement_experiments
    DROP COLUMN IF EXISTS treatment_prompt;

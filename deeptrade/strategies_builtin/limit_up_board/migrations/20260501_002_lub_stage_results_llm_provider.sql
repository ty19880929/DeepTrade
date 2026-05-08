-- v0.8 — debate mode: distinguish per-LLM rows in lub_stage_results.
-- llm_provider is NULL for non-debate runs and for cross-provider rows;
-- in debate mode each provider's r1 / r2_initial / r2_revised /
-- r2_final_initial rows are tagged with the provider name (e.g. 'deepseek').
ALTER TABLE lub_stage_results ADD COLUMN llm_provider VARCHAR;

CREATE INDEX IF NOT EXISTS ix_lub_stage_results_run_provider
    ON lub_stage_results(run_id, llm_provider, stage);
